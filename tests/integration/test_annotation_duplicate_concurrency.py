"""Test duplicate prevention when separate database transactions run concurrently."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Barrier, Event
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationStatus,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
    DuplicateAnnotationError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    create_unit_of_work_factory,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationService,
    RequestContext,
)

GUARD_SECONDS = 5
OBSERVATION_SECONDS = 2
Mutation = Callable[[Session], None]


def _changed(annotation: Annotation, **changes: object) -> Annotation:
    data = annotation.model_dump(mode="json")
    data.update(changes)
    return Annotation.model_validate(data)


def _create(annotation: Annotation) -> Mutation:
    def mutate(session: Session) -> None:
        AnnotationRepository(session).create_direct(
            annotation=annotation,
            actor_id="concurrent-creator",
            owning_group_id="group-1",
        )

    return mutate


def _update(annotation_id: UUID, annotation: Annotation) -> Mutation:
    def mutate(session: Session) -> None:
        AnnotationRepository(session).update_direct(
            annotation_id,
            annotation,
            expected_version=1,
            actor_id="concurrent-editor",
        )

    return mutate


def _prepare_session(session: Session) -> int:
    # Connect and bound every database operation before submitting workers.
    session.execute(text("SET LOCAL lock_timeout = '4s'"))
    session.execute(text("SET LOCAL statement_timeout = '4s'"))
    pid = session.scalar(text("SELECT pg_backend_pid()"))
    assert pid is not None
    return pid


def _run_coordinated_mutations(
    session_factory: sessionmaker[Session],
    first_mutation: Mutation,
    second_mutation: Mutation,
    *,
    preload_id: UUID | None = None,
    expect_overlap: bool = False,
) -> tuple[list[str], bool]:
    start = Barrier(2)
    first_flushed = Event()
    second_finished = Event()
    release = Event()

    def worker(session: Session, mutation: Mutation, *, first: bool) -> str:
        try:
            start.wait(timeout=GUARD_SECONDS)
            if not first:
                assert first_flushed.wait(GUARD_SECONDS), "first write did not flush"
            mutation(session)
            if first:
                first_flushed.set()
                assert release.wait(GUARD_SECONDS), "first transaction not released"
            elif expect_overlap:
                second_finished.set()
                assert release.wait(GUARD_SECONDS), "second transaction not released"
            session.commit()
            return "success"
        except DuplicateAnnotationError:
            session.rollback()
            return "duplicate"
        except StaleAnnotationVersionError as error:
            session.rollback()
            assert error.expected_version == 1
            assert error.current_version == 2
            return "stale"
        finally:
            if not first:
                second_finished.set()
            # Release failed transactions even if a statement or event times out.
            session.rollback()

    with (
        session_factory() as first_session,
        session_factory() as second_session,
        session_factory() as observer,
    ):
        first_pid = _prepare_session(first_session)
        second_pid = _prepare_session(second_session)
        observer_pid = _prepare_session(observer)
        assert len({first_pid, second_pid, observer_pid}) == 3
        assert first_session.scalar(text("SHOW transaction_isolation")) == (
            "read committed"
        )
        assert second_session.scalar(text("SHOW transaction_isolation")) == (
            "read committed"
        )
        # Keep the stale ORM object alive throughout the race: omitting the
        # repository's populate_existing refresh must not silently pass.
        cached = (
            AnnotationRepository(second_session).get(preload_id)
            if preload_id is not None
            else None
        )
        if preload_id is not None:
            assert cached is not None and cached.current_version == 1

        executor = ThreadPoolExecutor(max_workers=2)
        futures: list[Future[str]] = []
        failures: list[BaseException] = []
        blocked = False
        try:
            futures.append(
                executor.submit(worker, first_session, first_mutation, first=True)
            )
            futures.append(
                executor.submit(worker, second_session, second_mutation, first=False)
            )
            assert first_flushed.wait(GUARD_SECONDS), "first write did not flush"
            if expect_overlap:
                assert second_finished.wait(OBSERVATION_SECONDS), (
                    "unrelated write did not reach its post-lock, pre-commit checkpoint"
                )
                # Both writes are still uncommitted. Observe compatible global
                # locks as well as their independent signature locks in PostgreSQL.
                locks = tuple(
                    observer.execute(
                        text(
                            "SELECT pid, mode FROM pg_locks "
                            "WHERE locktype = 'advisory' AND granted "
                            "AND pid IN (:first, :second)"
                        ),
                        {"first": first_pid, "second": second_pid},
                    )
                )
                assert sorted(locks) == sorted(
                    [
                        (first_pid, "ShareLock"),
                        (first_pid, "ExclusiveLock"),
                        (second_pid, "ShareLock"),
                        (second_pid, "ExclusiveLock"),
                    ]
                )
            else:
                deadline = monotonic() + OBSERVATION_SECONDS
                while monotonic() < deadline:
                    blockers = observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                    )
                    if first_pid in blockers:
                        blocked = True
                        break
                    if second_finished.is_set():
                        break
                assert blocked or second_finished.is_set(), (
                    "second worker neither reached its checkpoint nor waited on first"
                )
        except BaseException as error:
            failures.append(error)
        finally:
            release.set()
            start.abort()
            _done, overdue = wait(futures, timeout=GUARD_SECONDS)
            if overdue:
                # Worker statements and event waits are bounded. Always drain
                # them before executor shutdown and before closing sessions.
                wait(overdue)
                failures.append(AssertionError("database worker exceeded its deadline"))
            executor.shutdown(wait=True, cancel_futures=True)

        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("coordination and worker failures", failures)
        return sorted(outcomes), blocked


def test_conflicting_creates_cannot_both_succeed(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    # Removing create_direct's signature lock allows both uncommitted creates.
    outcomes, blocked = _run_coordinated_mutations(
        session_factory, _create(validated_annotation), _create(validated_annotation)
    )
    assert outcomes == ["duplicate", "success"]
    assert blocked
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnnotationRecord)
                .where(AnnotationRecord.status == AnnotationStatus.ACTIVE.value)
            )
            == 1
        )


def test_distinct_record_updates_cannot_introduce_the_same_duplicate(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    with session_factory() as session:
        repository = AnnotationRepository(session)
        first = repository.create_direct(
            annotation=_changed(validated_annotation, db_object_id="UniProtKB:FIRST"),
            actor_id="seed",
            owning_group_id="group-1",
        )
        second = repository.create_direct(
            annotation=_changed(validated_annotation, db_object_id="UniProtKB:SECOND"),
            actor_id="seed",
            owning_group_id="group-1",
        )
        session.commit()
        first_id, second_id = first.annotation_id, second.annotation_id

    # Removing update_direct's candidate signature lock permits both updates.
    outcomes, blocked = _run_coordinated_mutations(
        session_factory,
        _update(first_id, validated_annotation),
        _update(second_id, validated_annotation),
    )
    assert outcomes == ["duplicate", "success"]
    assert blocked
    with session_factory() as session:
        records = tuple(session.scalars(select(AnnotationRecord)))
        assert sorted(record.current_version for record in records) == [1, 2]
        assert sum(record.db_object_id == "UniProtKB:P12345" for record in records) == 1
        assert all(record.status == "active" for record in records)


def test_same_record_update_race_rejects_the_stale_version(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    with session_factory() as session:
        record = AnnotationRepository(session).create_direct(
            annotation=validated_annotation, actor_id="seed", owning_group_id="group-1"
        )
        session.commit()
        annotation_id = record.annotation_id

    # Removing the fresh-state read leaves a cached version 1 in the waiter.
    outcomes, blocked = _run_coordinated_mutations(
        session_factory,
        _update(annotation_id, _changed(validated_annotation, assigned_by="First")),
        _update(annotation_id, _changed(validated_annotation, assigned_by="Second")),
        preload_id=annotation_id,
    )
    assert outcomes == ["stale", "success"]
    assert blocked
    with session_factory() as session:
        repository = AnnotationRepository(session)
        current = repository.get(annotation_id)
        assert current is not None
        assert current.current_version == 2
        assert current.assigned_by == "First"
        assert [
            version.version for version in repository.list_versions(annotation_id)
        ] == [
            1,
            2,
        ]


def test_patch_rejects_a_merge_based_on_a_different_version(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    with session_factory() as session:
        record = AnnotationRepository(session).create_direct(
            annotation=validated_annotation,
            actor_id="seed",
            owning_group_id="group-1",
        )
        session.commit()
        annotation_id = record.annotation_id

    writer_calls = 0

    def reader_session_factory() -> Session:
        reader = session_factory()

        def commit_expected_version_after_initial_read(*_args: object) -> None:
            nonlocal writer_calls
            writer_calls += 1
            with session_factory() as writer:
                AnnotationRepository(writer).update_direct(
                    annotation_id,
                    _changed(validated_annotation, assigned_by="Concurrent"),
                    expected_version=1,
                    actor_id="concurrent-editor",
                )
                writer.commit()

        event.listen(
            reader.connection(),
            "after_cursor_execute",
            commit_expected_version_after_initial_read,
            once=True,
        )
        return reader

    service = AnnotationService(create_unit_of_work_factory(reader_session_factory))
    with pytest.raises(StaleAnnotationVersionError):
        service.patch(
            annotation_id,
            changes={"annotation_date": "2026-09-10"},
            expected_version=2,
            context=RequestContext(actor_id="api-editor"),
        )

    assert writer_calls == 1
    with session_factory() as session:
        repository = AnnotationRepository(session)
        current = repository.get(annotation_id)
        assert current is not None
        current_version = current.current_version
        assigned_by = current.assigned_by
        versions = tuple(repository.list_versions(annotation_id))
    assert current_version == 2
    assert assigned_by == "Concurrent"
    assert [version.version for version in versions] == [1, 2]


def test_unrelated_signatures_overlap_after_the_shared_global_lock(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    # An exclusive global lock or constant signature key prevents the second
    # post-lock checkpoint while the first transaction remains uncommitted.
    outcomes, _blocked = _run_coordinated_mutations(
        session_factory,
        _create(validated_annotation),
        _create(_changed(validated_annotation, db_object_id="UniProtKB:UNRELATED")),
        expect_overlap=True,
    )
    assert outcomes == ["success", "success"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 2
