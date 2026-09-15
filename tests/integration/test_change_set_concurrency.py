"""Verify change-set review races using independent PostgreSQL transactions."""

import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Barrier, Event, Timer
from time import monotonic
from typing import Never
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    ChangeSetRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
    ChangeSetRepository,
    DuplicateAnnotationError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    UnitOfWorkFactory,
    create_unit_of_work_factory,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationService,
    RequestContext,
)
from standard_annotation_backend.services.change_set_service import (
    AcceptedChangeSet,
    ChangeSetService,
    ChangeSetStateError,
    StaleChangeSetError,
)

GUARD_SECONDS = 10
OBSERVATION_SECONDS = 3
PROPOSER = RequestContext(actor_id="proposer")
FIRST_REVIEWER = RequestContext(actor_id="first-reviewer")
SECOND_REVIEWER = RequestContext(actor_id="second-reviewer")
Operation = Callable[[UnitOfWorkFactory], object]


def _prepare_session(session: Session) -> int:
    """Connect each participant and bound its database statements and lock waits."""
    session.execute(text("SET LOCAL lock_timeout = '8s'"))
    session.execute(text("SET LOCAL statement_timeout = '8s'"))
    assert session.scalar(text("SHOW transaction_isolation")) == "read committed"
    pid = session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


def _blocked_by(
    observer: Session, *, waiter: int, holder: int, finished: Event
) -> bool:
    """Observe an actual PostgreSQL blocker, with an event-bounded polling guard."""
    deadline = monotonic() + OBSERVATION_SECONDS
    while monotonic() < deadline:
        blockers = observer.scalar(
            text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
        )
        if holder in blockers:
            return True
        if finished.wait(0.01):
            break
    return False


def _abort_stalled_runner() -> Never:
    """Stop all threads without running fixture teardown after cleanup stalls."""
    os.write(2, b"database worker cleanup exceeded its deadline; skipping teardown\n")
    os._exit(1)


def _drain_workers(
    executor: ThreadPoolExecutor,
    futures: list[Future[object]],
    failures: list[BaseException],
    *,
    stop_workers: Callable[[], None] | None = None,
) -> list[object]:
    """Drain workers before teardown, stopping the runner if cleanup cannot finish.

    Python cannot safely terminate individual threads. The outer deadline also
    covers cancellation and executor shutdown, which can stall in client code.
    Exiting the runner is the last resort that prevents teardown with live workers.
    """
    deadline = Timer(2 * GUARD_SECONDS, _abort_stalled_runner)
    deadline.daemon = True
    deadline.start()
    try:
        _done, overdue = wait(futures, timeout=GUARD_SECONDS)
        if overdue:
            failures.append(AssertionError("database worker exceeded its deadline"))
            if stop_workers is not None:
                try:
                    stop_workers()
                except BaseException as error:
                    failures.append(error)
            for future in overdue:
                future.cancel()
            _done, overdue = wait(overdue, timeout=GUARD_SECONDS)
            if overdue:
                _abort_stalled_runner()
        executor.shutdown(wait=True, cancel_futures=True)
    finally:
        deadline.cancel()
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
    return outcomes


def _run_review_race(
    session_factory: sessionmaker[Session],
    first_operation: Operation,
    second_operation: Operation,
    *,
    preload_annotation_id: UUID | None = None,
    preload_change_set_id: UUID | None = None,
    expect_overlap: bool = False,
) -> tuple[list[object], bool]:
    """Hold the first service before commit while the second competes or overlaps."""
    start = Barrier(2)
    first_ready = Event()
    second_ready = Event()
    second_finished = Event()
    release = Event()

    def worker(session: Session, operation: Operation, *, first: bool) -> object:
        def before_commit(committing: Session) -> None:
            committing.flush()
            (first_ready if first else second_ready).set()
            assert release.wait(GUARD_SECONDS), "transaction was not released"

        event.listen(session, "before_commit", before_commit, once=True)
        try:
            start.wait(timeout=GUARD_SECONDS)
            if not first:
                assert first_ready.wait(GUARD_SECONDS), "first write did not flush"
            return operation(create_unit_of_work_factory(lambda: session))
        except (
            ChangeSetStateError,
            DuplicateAnnotationError,
            StaleChangeSetError,
        ) as err:
            return err
        finally:
            session.rollback()
            if not first:
                second_finished.set()

    with (
        session_factory() as first_session,
        session_factory() as second_session,
        session_factory() as observer,
    ):
        first_pid = _prepare_session(first_session)
        second_pid = _prepare_session(second_session)
        observer_pid = _prepare_session(observer)
        assert len({first_pid, second_pid, observer_pid}) == 3
        # Keep these ORM objects alive so a lock without populate_existing cannot
        # conceal its stale read by simply loading a new Python object.
        cached_annotation = (
            AnnotationRepository(second_session).get(preload_annotation_id)
            if preload_annotation_id is not None
            else None
        )
        if preload_annotation_id is not None:
            assert cached_annotation is not None
            assert cached_annotation.current_version == 1
        cached_proposal = (
            ChangeSetRepository(second_session).get(preload_change_set_id)
            if preload_change_set_id is not None
            else None
        )
        if preload_change_set_id is not None:
            assert cached_proposal is not None
            assert cached_proposal.state == "proposed"

        executor = ThreadPoolExecutor(max_workers=2)
        futures: list[Future[object]] = []
        failures: list[BaseException] = []
        blocked = False

        def stop_workers() -> None:
            release.set()
            first_ready.set()
            start.abort()
            for pid in (first_pid, second_pid):
                observer.execute(text("SELECT pg_cancel_backend(:pid)"), {"pid": pid})

        try:
            futures.append(
                executor.submit(worker, first_session, first_operation, first=True)
            )
            futures.append(
                executor.submit(worker, second_session, second_operation, first=False)
            )
            assert first_ready.wait(GUARD_SECONDS), "first write did not flush"
            if expect_overlap:
                assert second_ready.wait(OBSERVATION_SECONDS), (
                    "unrelated acceptance did not reach its pre-commit checkpoint"
                )
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
                blocked = _blocked_by(
                    observer,
                    waiter=second_pid,
                    holder=first_pid,
                    finished=second_finished,
                )
                assert blocked or second_ready.is_set() or second_finished.is_set(), (
                    "second review neither reached a checkpoint nor waited on first"
                )
        except BaseException as error:
            failures.append(error)
        finally:
            release.set()
            start.abort()
            outcomes = _drain_workers(
                executor, futures, failures, stop_workers=stop_workers
            )
        return outcomes, blocked


def _propose_create(factory: UnitOfWorkFactory, annotation: Annotation) -> UUID:
    proposed = ChangeSetService(factory).propose_create(
        payload=annotation.model_dump(mode="json"),
        owning_group_id="curator-group",
        reason="Review new evidence",
        context=PROPOSER,
    )
    return proposed.change_set_id


def _accept(change_set_id: UUID, context: RequestContext) -> Operation:
    def accept(factory: UnitOfWorkFactory) -> AcceptedChangeSet:
        return ChangeSetService(factory).accept(change_set_id, context=context)

    return accept


def test_two_reviewers_accept_one_proposal_only_once(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Competing reviewers produce one annotation, transition, and acceptance audit."""
    proposal_id = _propose_create(unit_of_work_factory, validated_annotation)
    # Dropping the proposal row lock lets the waiter apply a still-proposed copy;
    # dropping the fresh read leaves its preloaded review state out of date.
    outcomes, blocked = _run_review_race(
        session_factory,
        _accept(proposal_id, FIRST_REVIEWER),
        _accept(proposal_id, SECOND_REVIEWER),
        preload_change_set_id=proposal_id,
    )
    accepted, error = outcomes
    assert isinstance(accepted, AcceptedChangeSet)
    assert isinstance(error, ChangeSetStateError)
    assert error.change_set_id == proposal_id
    assert error.state == "accepted"
    assert blocked
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 1
        assert (
            session.scalar(select(func.count()).select_from(AnnotationVersionRecord))
            == 1
        )
        proposal = session.get(ChangeSetRecord, proposal_id)
        assert proposal is not None
        assert proposal.state == "accepted"
        assert proposal.reviewed_by == FIRST_REVIEWER.actor_id
        assert proposal.annotation_id == accepted.annotation_id
        assert proposal.result_annotation_version == 1
        events = tuple(session.scalars(select(AuditEventRecord)))
        assert sorted(item.action for item in events) == [
            "change_set.accepted",
            "change_set.proposed",
        ]
        acceptance = next(
            item for item in events if item.action == "change_set.accepted"
        )
        assert acceptance.actor_id == FIRST_REVIEWER.actor_id
        assert acceptance.annotation_id == accepted.annotation_id
        assert acceptance.annotation_version == 1


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_direct_update_makes_waiting_acceptance_stale_without_overwriting(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """A waiting acceptance commits staleness and preserves the direct writer's data."""
    payload = validated_annotation.model_dump(mode="json")
    created = AnnotationService(unit_of_work_factory).create(
        payload=payload, owning_group_id="group", context=PROPOSER
    )
    service = ChangeSetService(unit_of_work_factory)
    proposed = (
        service.propose_update(
            created.annotation_id,
            base_version=1,
            patch=[{"op": "replace", "path": "/assigned_by", "value": "Proposed"}],
            reason="Review edit",
            context=PROPOSER,
        )
        if operation == "update"
        else service.propose_delete(
            created.annotation_id,
            base_version=1,
            reason="Review deletion",
            context=PROPOSER,
        )
    )

    def direct_update(factory: UnitOfWorkFactory) -> object:
        return AnnotationService(factory).patch(
            created.annotation_id,
            changes={"assigned_by": "Direct"},
            expected_version=1,
            context=FIRST_REVIEWER,
        )

    # Without the repository's populate_existing read, the cached version 1 can
    # survive the row-lock wait instead of committing the proposal's stale state.
    outcomes, blocked = _run_review_race(
        session_factory,
        direct_update,
        _accept(proposed.change_set_id, SECOND_REVIEWER),
        preload_annotation_id=created.annotation_id,
    )
    error = outcomes[1]
    assert isinstance(error, StaleChangeSetError)
    assert error.change_set_id == proposed.change_set_id
    assert error.annotation_id == created.annotation_id
    assert (error.expected_version, error.current_version) == (1, 2)
    assert blocked
    with session_factory() as session:
        current = session.get(AnnotationRecord, created.annotation_id)
        assert current is not None
        assert current.status == "active"
        assert current.current_version == 2
        assert current.assigned_by == "Direct"
        versions = AnnotationRepository(session).list_versions(created.annotation_id)
        assert [version.version for version in versions] == [1, 2]
        assert versions[-1].annotation_data["assigned_by"] == "Direct"
        assert not versions[-1].is_deleted
        proposal = session.get(ChangeSetRecord, proposed.change_set_id)
        assert proposal is not None
        assert proposal.state == "stale"
        assert proposal.reviewed_by == SECOND_REVIEWER.actor_id
        assert proposal.reviewed_at is not None
        assert proposal.result_annotation_version is None
        assert proposal.preview is not None
        assert proposal.preview["is_stale"] is True
        assert proposal.preview["current_version"] == 2
        events = tuple(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.change_set_id == proposed.change_set_id
                )
            )
        )
        assert sorted(item.action for item in events) == [
            "change_set.marked_stale",
            "change_set.proposed",
        ]
        stale = next(
            item for item in events if item.action == "change_set.marked_stale"
        )
        assert stale.actor_id == SECOND_REVIEWER.actor_id
        assert stale.annotation_id == created.annotation_id
        assert stale.annotation_version == 2


def test_same_payload_create_acceptances_cannot_both_succeed(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Conflicting acceptances create one annotation and keep the losing proposal open."""
    first_id = _propose_create(unit_of_work_factory, validated_annotation)
    second_id = _propose_create(unit_of_work_factory, validated_annotation)
    # Removing create_direct's signature lock lets both transactions find no peer.
    outcomes, blocked = _run_review_race(
        session_factory,
        _accept(first_id, FIRST_REVIEWER),
        _accept(second_id, SECOND_REVIEWER),
    )
    accepted, error = outcomes
    assert isinstance(accepted, AcceptedChangeSet)
    assert isinstance(error, DuplicateAnnotationError)
    assert error.peer_ids == (accepted.annotation_id,)
    assert blocked
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 1
        assert (
            session.scalar(select(func.count()).select_from(AnnotationVersionRecord))
            == 1
        )
        second = session.get(ChangeSetRecord, second_id)
        assert second is not None
        assert second.state == "proposed"
        assert second.reviewed_at is None
        assert second.annotation_id is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == "change_set.accepted")
            )
            == 1
        )


def test_unrelated_create_acceptances_overlap_after_shared_global_lock(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Unrelated acceptances hold compatible global locks before either commits."""
    first_id = _propose_create(unit_of_work_factory, validated_annotation)
    unrelated = Annotation.model_validate(
        {
            **validated_annotation.model_dump(mode="json"),
            "db_object_id": "UniProtKB:OTHER",
        }
    )
    second_id = _propose_create(unit_of_work_factory, unrelated)
    # An exclusive global lock or a constant signature key prevents overlap.
    outcomes, _blocked = _run_review_race(
        session_factory,
        _accept(first_id, FIRST_REVIEWER),
        _accept(second_id, SECOND_REVIEWER),
        expect_overlap=True,
    )
    assert all(isinstance(outcome, AcceptedChangeSet) for outcome in outcomes)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 2
        assert list(session.scalars(select(ChangeSetRecord.state))) == [
            "accepted",
            "accepted",
        ]


def test_conflicting_update_acceptances_cannot_both_succeed(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Updates of different targets serialize before introducing a shared duplicate."""
    annotation_service = AnnotationService(unit_of_work_factory)
    change_sets = ChangeSetService(unit_of_work_factory)
    proposal_ids = []
    target_ids = []
    for db_object_id in ("UniProtKB:FIRST", "UniProtKB:SECOND"):
        created = annotation_service.create(
            payload={
                **validated_annotation.model_dump(mode="json"),
                "db_object_id": db_object_id,
            },
            owning_group_id="group",
            context=PROPOSER,
        )
        proposed = change_sets.propose_update(
            created.annotation_id,
            base_version=1,
            patch=[
                {"op": "replace", "path": "/db_object_id", "value": "UniProtKB:P12345"}
            ],
            reason="Correct the annotated gene product",
            context=PROPOSER,
        )
        target_ids.append(created.annotation_id)
        proposal_ids.append(proposed.change_set_id)

    # Removing update_direct's signature locks permits both unrelated row locks
    # to check the candidate before the other's uncommitted write is visible.
    outcomes, blocked = _run_review_race(
        session_factory,
        _accept(proposal_ids[0], FIRST_REVIEWER),
        _accept(proposal_ids[1], SECOND_REVIEWER),
    )
    accepted, error = outcomes
    assert isinstance(accepted, AcceptedChangeSet)
    assert accepted.annotation_id == target_ids[0]
    assert accepted.version == 2
    assert isinstance(error, DuplicateAnnotationError)
    assert error.peer_ids == (target_ids[0],)
    assert blocked
    with session_factory() as session:
        first = session.get(AnnotationRecord, target_ids[0])
        second = session.get(AnnotationRecord, target_ids[1])
        assert first is not None and second is not None
        assert (first.db_object_id, first.current_version) == ("UniProtKB:P12345", 2)
        assert (second.db_object_id, second.current_version) == ("UniProtKB:SECOND", 1)
        assert first.status == second.status == "active"
        assert (
            session.scalar(select(func.count()).select_from(AnnotationVersionRecord))
            == 3
        )
        proposal = session.get(ChangeSetRecord, proposal_ids[1])
        assert proposal is not None
        assert proposal.state == "proposed"
        assert proposal.reviewed_at is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == "change_set.accepted")
            )
            == 1
        )


@pytest.mark.parametrize("operation", ["accept", "preview"])
def test_review_and_preview_wait_for_global_lock_before_locking_proposal(
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """An exclusive global holder can still lock the proposal while review waits."""
    proposal_id = _propose_create(unit_of_work_factory, validated_annotation)
    started = Event()
    finished = Event()
    with (
        session_factory() as holder,
        session_factory() as reviewer,
        session_factory() as observer,
    ):
        holder_pid = _prepare_session(holder)
        reviewer_pid = _prepare_session(reviewer)
        observer_pid = _prepare_session(observer)
        assert len({holder_pid, reviewer_pid, observer_pid}) == 3
        acquire_global_annotation_write_lock(holder, exclusive=True)

        def review() -> object:
            started.set()
            service = ChangeSetService(create_unit_of_work_factory(lambda: reviewer))
            try:
                if operation == "accept":
                    return service.accept(proposal_id, context=FIRST_REVIEWER)
                return service.preview(proposal_id)
            finally:
                reviewer.rollback()
                finished.set()

        executor = ThreadPoolExecutor(max_workers=1)
        futures: list[Future[object]] = []
        failures: list[BaseException] = []

        def stop_reviewer() -> None:
            observer.execute(
                text("SELECT pg_cancel_backend(:pid)"), {"pid": reviewer_pid}
            )

        try:
            futures.append(executor.submit(review))
            assert started.wait(GUARD_SECONDS), "review worker did not start"
            assert _blocked_by(
                observer, waiter=reviewer_pid, holder=holder_pid, finished=finished
            ), "review did not wait on the exclusive global holder"
            # Moving the global lock after SELECT FOR UPDATE makes this NOWAIT
            # fail: a bootstrap holder must be able to remove dependent rows.
            row = holder.scalar(
                select(ChangeSetRecord)
                .where(ChangeSetRecord.change_set_id == proposal_id)
                .with_for_update(nowait=True)
            )
            assert row is not None
            assert row.state == "proposed"
            assert row.preview is None
        except BaseException as error:
            failures.append(error)
        finally:
            holder.rollback()
            _drain_workers(executor, futures, failures, stop_workers=stop_reviewer)
    stored = ChangeSetService(unit_of_work_factory).get(proposal_id)
    assert stored.state == ("accepted" if operation == "accept" else "proposed")
    assert stored.preview is not None
