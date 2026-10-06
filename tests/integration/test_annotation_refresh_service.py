"""Verify what GPAD refresh and cutover jobs publish, skip, and delete."""

from collections.abc import Callable
from uuid import UUID

import pytest
from annotation_refresh_helpers import gpad_bytes, gpad_row, gpad_text
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    source_document,
    stage_without_publishing,
    start_job,
)
from sqlalchemy import ColumnElement, Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
    CutoverRejectedError,
    GroupSabManagedError,
)
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    ChangeSetRecord,
    EntityMembershipRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationImportRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)

GROUP = "MGI"
SOURCE = "mgi-gpad"
GOOD = gpad_bytes(gpad_row("UniProtKB:P12345"))

RunJob = Callable[..., Job]


def create_job(
    unit_of_work_factory: UnitOfWorkFactory,
    job_type: JobType = JobType.ANNOTATION_REFRESH,
    source_key: str = SOURCE,
) -> Job:
    """Create and start a GPAD job requested by `curator`, and return it."""
    return start_job(unit_of_work_factory, job_type, source_key)


def stage(service: AnnotationRefreshService, job: Job, text: str) -> None:
    """Commit staging for `job` without publishing, as an interrupted worker would."""
    stage_without_publishing(
        service,
        job,
        source_document(SOURCE, text.encode()),
        (AnnotationImportRepository, "publish"),
    )


@pytest.fixture
def run(database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory) -> RunJob:
    """Return a function that runs one GPAD job to completion and returns it.

    The function takes the fetched document's bytes, and optionally `source` and
    `job_type`. The job is requested by `curator`.
    """
    jobs = JobService(unit_of_work_factory)

    def run_job(
        content: bytes,
        *,
        source: str = SOURCE,
        job_type: JobType = JobType.ANNOTATION_REFRESH,
    ) -> Job:
        job_id = jobs.create(
            job_type=job_type,
            requested_by="curator",
            parameters={"source_key": source},
        ).job_id
        build_runner(
            database_engine, unit_of_work_factory, FakeFetchers({source: content})
        ).run(job_id)
        return jobs.find(job_id)

    return run_job


def annotations_of(
    session_factory: sessionmaker[Session], group: str = GROUP
) -> list[AnnotationRecord]:
    """Return a group's current annotations."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(AnnotationRecord).where(
                    AnnotationRecord.owning_group_id == group
                )
            )
        )


def _annotation(db_object_id: str, assigned_by: str = GROUP) -> Annotation:
    """Return a valid annotation of `db_object_id`."""
    return Annotation.model_validate(
        {
            "db_object_id": db_object_id,
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:99"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-09-29",
            "assigned_by": assigned_by,
        }
    )


def _create_proposal(
    unit_of_work_factory: UnitOfWorkFactory, group: str = GROUP
) -> UUID:
    """Propose a new annotation for `group`, audit the proposal, and return its ID."""
    with unit_of_work_factory() as uow:
        proposal = uow.change_sets.create(
            operation="create",
            proposed_by="curator",
            reason="New finding",
            owning_group_id=group,
            annotation_payload=_annotation("UniProtKB:P12345", group).model_dump(
                mode="json"
            ),
        )
        uow.audit.record(
            action=AuditAction.CHANGE_SET_PROPOSED,
            actor_id="curator",
            result=AuditResult.SUCCESS,
            change_set_id=proposal.change_set_id,
        )
        uow.commit()
        return proposal.change_set_id


def _update_proposal(unit_of_work_factory: UnitOfWorkFactory, annotation: UUID) -> UUID:
    """Propose an edit to `annotation`, audit the proposal, and return its ID."""
    with unit_of_work_factory() as uow:
        proposal = uow.change_sets.create(
            operation="update",
            proposed_by="curator",
            reason="Fix reference",
            annotation_id=annotation,
            base_version=1,
            patch=[{"op": "replace", "path": "/references/0", "value": "PMID:5"}],
        )
        uow.audit.record(
            action=AuditAction.CHANGE_SET_PROPOSED,
            actor_id="curator",
            result=AuditResult.SUCCESS,
            change_set_id=proposal.change_set_id,
        )
        uow.commit()
        return proposal.change_set_id


def _audit_count(
    session_factory: sessionmaker[Session], *conditions: ColumnElement[bool]
) -> int:
    """Return the number of audit events matching every condition."""
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(AuditEventRecord).where(*conditions)
            )
            or 0
        )


def test_refresh_publishes_valid_annotations_and_reports_rejected_records(
    run: RunJob,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """Valid annotations are published and every rejected record is reported.

    A row with with/from alternatives becomes one annotation per alternative.
    Unknown subjects and unreadable rows are reported by line number and code in
    the job result.
    """
    seed_active_subjects("UniProtKB:P12345")

    job = run(
        gpad_bytes(
            gpad_row("UniProtKB:P12345", with_or_from="UniProtKB:Q1|UniProtKB:Q2"),
            gpad_row("UniProtKB:UNKNOWN"),
            "too\tfew",
        )
    )

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    report = job.result.pop("rejection_report")
    assert job.result == {
        "source_key": SOURCE,
        "group_key": GROUP,
        "import_job_id": str(job.job_id),
        "is_cutover": False,
        "source_type": "https",
        "source_locator": "https://example.org/mgi.gpad",
        "source_revision": None,
        "source_checksum": job.result["source_checksum"],
        "fetched_at": "2026-10-01T00:00:00+00:00",
        "data_rows": 3,
        "annotations_published": 2,
        "records_rejected": 2,
        "annotations_deleted": 0,
        "mode": "gpad_imported",
        "unchanged": False,
    }
    assert isinstance(report, dict)
    assert report["issue_count"] == 2
    assert report["by_code"] == {"unknown_db_object_id": 1, "field-count": 1}
    issues = report["issues"]
    assert isinstance(issues, list)
    assert [(issue["line_number"], issue["code"]) for issue in issues] == [
        (5, "unknown_db_object_id"),
        (6, "field-count"),
    ]
    assert "UniProtKB:UNKNOWN" in issues[0]["reason"]
    assert sorted(
        (
            record.annotation_data["with_or_from"]
            for record in annotations_of(session_factory)
        ),
        key=str,
    ) == [["UniProtKB:Q1"], ["UniProtKB:Q2"]]


def test_rerun_after_interrupted_staging_publishes_only_the_fetched_document(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A job resumed after staging but before publishing imports the new fetch only.

    Nothing staged by the interrupted attempt appears among the published
    annotations.
    """
    seed_active_subjects("UniProtKB:P12345")
    jobs = JobService(unit_of_work_factory)
    job = create_job(unit_of_work_factory)
    stage(
        AnnotationRefreshService(
            unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
        ),
        job,
        gpad_text(gpad_row("UniProtKB:P12345", reference="PMID:1")),
    )

    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers(
            {
                SOURCE: gpad_bytes(
                    gpad_row("UniProtKB:P12345", reference="PMID:2"),
                    gpad_row("UniProtKB:P12345", reference="PMID:3"),
                )
            }
        ),
    ).run(job.job_id)

    job = jobs.find(job.job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None and job.result["annotations_published"] == 2
    assert sorted(
        (
            record.annotation_data["references"]
            for record in annotations_of(session_factory)
        ),
        key=str,
    ) == [["PMID:2"], ["PMID:3"]]


def _apply_local_change(
    change: str, unit_of_work_factory: UnitOfWorkFactory, imported: UUID
) -> None:
    """Make one kind of SAB-local change to the test group."""
    if change == "change_set":
        _create_proposal(unit_of_work_factory)
        return
    with unit_of_work_factory() as uow:
        if change == "direct_create":
            uow.annotations.create_direct(
                annotation=_annotation("UniProtKB:Q99999"),
                actor_id="curator",
                owning_group_id=GROUP,
            )
        elif change == "edit":
            uow.annotations.update(
                imported,
                _annotation("UniProtKB:Q99999"),
                actor_id="curator",
                change_source="api",
            )
        elif change == "soft_delete":
            uow.annotations.soft_delete(
                imported, actor_id="curator", change_source="api"
            )
        else:
            uow.comments.create(imported, body="Looks wrong", created_by="curator")
        uow.commit()


@pytest.mark.parametrize(
    "change", ["direct_create", "edit", "soft_delete", "comment", "change_set"]
)
def test_any_local_change_forces_an_identical_file_to_be_imported(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
    change: str,
) -> None:
    """After a local change, refreshing the same file replaces the group again.

    Direct creates, edits, soft deletes, comments, and change sets each count.
    """
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q99999")
    run(GOOD)
    [imported] = annotations_of(session_factory)
    _apply_local_change(change, unit_of_work_factory, imported.annotation_id)

    again = run(GOOD)

    assert again.status is JobStatus.SUCCEEDED
    assert again.progress["unchanged"] is False
    assert again.result is not None and again.result["unchanged"] is False
    assert again.result["annotations_published"] == 1
    [published] = annotations_of(session_factory)
    assert (published.source_import_job_id, published.current_version) == (
        again.job_id,
        1,
    )


def test_changed_file_is_imported(
    run: RunJob,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A file whose bytes differ from the last import is not skipped."""
    seed_active_subjects("UniProtKB:P12345")
    run(GOOD)

    again = run(gpad_bytes(gpad_row("UniProtKB:P12345", reference="PMID:2")))

    assert again.progress["unchanged"] is False
    assert again.result is not None and again.result["unchanged"] is False
    assert [
        record.annotation_data["references"]
        for record in annotations_of(session_factory)
    ] == [["PMID:2"]]


def test_ontology_term_replacement_does_not_force_a_reimport(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """SAB's automatic term replacements leave an identical file skippable."""
    seed_active_subjects("UniProtKB:P12345")
    run(GOOD)
    [imported] = annotations_of(session_factory)
    with unit_of_work_factory() as uow:
        uow.annotations.update(
            imported.annotation_id,
            _annotation("UniProtKB:P12345").model_copy(
                update={"ontology_class_id": "GO:0003674"}
            ),
            actor_id="system",
            change_source="ontology_refresh",
        )
        uow.commit()

    again = run(GOOD)

    assert again.progress["unchanged"] is True
    assert again.result is not None and again.result["unchanged"] is True
    [kept] = annotations_of(session_factory)
    assert kept.annotation_data["ontology_class_id"] == "GO:0003674"


def test_identical_file_is_imported_again_after_unknown_entity_rejections(
    run: RunJob,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """Annotations rejected for an unknown subject are admitted once it is known.

    The file is unchanged, but the entity catalog changed after the first import.
    """
    seed_active_subjects("UniProtKB:P12345")
    content = gpad_bytes(gpad_row("UniProtKB:P12345"), gpad_row("UniProtKB:Q99999"))
    first = run(content)
    assert first.result is not None and first.result["records_rejected"] == 1
    seed_active_subjects("UniProtKB:Q99999")

    again = run(content)

    assert again.progress["unchanged"] is False
    assert again.result is not None and again.result["unchanged"] is False
    assert again.result["records_rejected"] == 0
    assert sorted(
        (
            record.annotation_data["db_object_id"]
            for record in annotations_of(session_factory)
        ),
        key=str,
    ) == ["UniProtKB:P12345", "UniProtKB:Q99999"]


def test_identical_file_with_only_unreadable_rows_rejected_is_skipped(
    run: RunJob, seed_active_subjects: Callable[..., None]
) -> None:
    """Rejections other than unknown subjects do not prevent the unchanged skip."""
    seed_active_subjects("UniProtKB:P12345")
    content = gpad_bytes(gpad_row("UniProtKB:P12345"), "too\tfew")
    first = run(content)
    assert first.result is not None and first.result["records_rejected"] == 1

    again = run(content)
    assert again.progress["unchanged"] is True
    assert again.result is not None and again.result["unchanged"] is True


def test_refresh_replaces_only_the_target_groups_annotations_and_history(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
    seed_annotation: Callable[[str], UUID],
) -> None:
    """A refresh deletes the group's direct annotations with their history.

    Edits, soft deletes, comments, and annotation audits go with them. The new
    annotations are version 1 of the import, attributed to the requester.
    Another group's annotations, both jobs, and both publication audits remain.
    """
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q99999")
    edited = seed_annotation("UniProtKB:Q99999")
    soft_deleted = seed_annotation("UniProtKB:Q99999")
    with unit_of_work_factory() as uow:
        uow.annotations.update(
            edited,
            _annotation("UniProtKB:Q99999"),
            actor_id="curator",
            change_source="api",
        )
        uow.annotations.soft_delete(
            soft_deleted, actor_id="curator", change_source="api"
        )
        uow.comments.create(edited, body="note", created_by="curator")
        for annotation_id in (edited, soft_deleted):
            uow.audit.record(
                action=AuditAction.ANNOTATION_UPDATED,
                actor_id="curator",
                result=AuditResult.SUCCESS,
                annotation_id=annotation_id,
            )
        uow.commit()
    rgd = run(GOOD, source="rgd-gpad")

    mgi = run(
        gpad_bytes(
            gpad_row("UniProtKB:P12345"),
            gpad_row("UniProtKB:P12345", reference="PMID:2"),
        )
    )

    assert mgi.result is not None
    assert (mgi.result["annotations_published"], mgi.result["annotations_deleted"]) == (
        2,
        2,
    )
    published = annotations_of(session_factory)
    assert {record.record_origin for record in published} == {"import"}
    assert {record.source_import_job_id for record in published} == {mgi.job_id}
    assert {record.current_version for record in published} == {1}
    assert [
        record.source_import_job_id for record in annotations_of(session_factory, "RGD")
    ] == [rgd.job_id]
    assert (
        _audit_count(
            session_factory, AuditEventRecord.annotation_id.in_([edited, soft_deleted])
        )
        == 0
    )
    with session_factory() as session:
        assert session.get(AnnotationRecord, soft_deleted) is None
        assert (
            session.scalar(select(func.count()).select_from(AnnotationCommentRecord))
            == 0
        )
        versions = session.scalars(
            select(AnnotationVersionRecord).where(
                AnnotationVersionRecord.annotation_id.in_(
                    [record.annotation_id for record in published]
                )
            )
        ).all()
        assert {(v.version, v.actor_id, v.change_source) for v in versions} == {
            (1, "curator", "annotation_refresh")
        }
        published_audits = session.scalars(
            select(AuditEventRecord.job_id).where(
                AuditEventRecord.action == AuditAction.ANNOTATION_REFRESH_PUBLISHED
            )
        ).all()
    assert set(published_audits) == {rgd.job_id, mgi.job_id}
    assert JobService(unit_of_work_factory).find(rgd.job_id).status is (
        JobStatus.SUCCEEDED
    )


def test_refresh_deletes_the_groups_change_sets_and_their_audits(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A refresh deletes the group's pending create and update proposals.

    The proposals' audit events are deleted with them.
    """
    seed_active_subjects("UniProtKB:P12345")
    run(GOOD)
    [imported] = annotations_of(session_factory)
    proposals = {
        _create_proposal(unit_of_work_factory),
        _update_proposal(unit_of_work_factory, imported.annotation_id),
    }

    run(GOOD)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeSetRecord)) == 0
    assert (
        _audit_count(session_factory, AuditEventRecord.change_set_id.in_(proposals))
        == 0
    )


def test_refresh_keeps_another_groups_comments_change_sets_and_audits(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """Refreshing one group leaves another group's dependents and audits intact."""
    seed_active_subjects("UniProtKB:P12345")
    run(GOOD, source="rgd-gpad")
    [rgd_annotation] = [
        record.annotation_id for record in annotations_of(session_factory, "RGD")
    ]
    with unit_of_work_factory() as uow:
        comment_id = uow.comments.create(
            rgd_annotation, body="note", created_by="curator"
        ).comment_id
        uow.audit.record(
            action=AuditAction.ANNOTATION_UPDATED,
            actor_id="curator",
            result=AuditResult.SUCCESS,
            annotation_id=rgd_annotation,
        )
        uow.commit()
    change_set_ids = {
        _create_proposal(unit_of_work_factory, "RGD"),
        _update_proposal(unit_of_work_factory, rgd_annotation),
    }
    rgd_audits = (
        AuditEventRecord.annotation_id == rgd_annotation
    ) | AuditEventRecord.change_set_id.in_(change_set_ids)
    assert _audit_count(session_factory, rgd_audits) == 3

    run(GOOD)

    assert [
        record.annotation_id for record in annotations_of(session_factory, "RGD")
    ] == [rgd_annotation]
    with session_factory() as session:
        assert session.get(AnnotationCommentRecord, comment_id) is not None
        assert {
            row.change_set_id
            for row in session.scalars(
                select(ChangeSetRecord).where(ChangeSetRecord.state == "proposed")
            )
        } == change_set_ids
    assert _audit_count(session_factory, rgd_audits) == 3


def test_identical_rows_in_one_file_are_all_published(
    run: RunJob,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """Identical GPAD rows become separate annotations without duplicate checks."""
    seed_active_subjects("UniProtKB:P12345")
    row = gpad_row("UniProtKB:P12345")

    job = run(gpad_bytes(row, row, row))

    assert job.result is not None and job.result["annotations_published"] == 3
    assert len(annotations_of(session_factory)) == 3


def test_cutover_moves_the_group_to_sab_management_permanently(
    run: RunJob,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A clean cutover publishes the file and makes the group SAB-managed.

    Afterward, neither a refresh nor another cutover can be requested for the
    group, and refreshing all sources leaves it out.
    """
    seed_active_subjects("UniProtKB:P12345")

    job = run(GOOD, job_type=JobType.ANNOTATION_CUTOVER)

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert (job.result["mode"], job.result["is_cutover"]) == ("sab_managed", True)
    with session_factory() as session:
        versions = session.execute(
            select(
                AnnotationVersionRecord.actor_id, AnnotationVersionRecord.change_source
            )
        ).all()
    assert [tuple(version) for version in versions] == [
        ("curator", "annotation_cutover")
    ]
    starts = RefreshStartService(unit_of_work_factory, TEST_SOURCES, lambda _job: None)
    with pytest.raises(GroupSabManagedError):
        starts.start(
            RefreshKindName.ANNOTATION, requested_by="admin", source_key=SOURCE
        )
    with pytest.raises(GroupSabManagedError):
        starts.start_cutover(requested_by="admin", source_key=SOURCE)
    refresh_all = starts.start(
        RefreshKindName.ANNOTATION, requested_by="scheduler", source_key=None
    )
    assert [job.parameters["source_key"] for job in refresh_all] == ["rgd-gpad"]


def test_cutover_fails_when_an_entity_disappears_before_publication(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A cutover fails at publication if a staged subject is no longer active.

    The group stays `gpad_imported` and its annotations are unchanged.
    """
    service = AnnotationRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
    )
    seed_active_subjects("UniProtKB:P12345")
    job = create_job(unit_of_work_factory, JobType.ANNOTATION_CUTOVER)
    stage(service, job, gpad_text(gpad_row("UniProtKB:P12345")))
    with session_factory() as session:
        session.execute(delete(EntityMembershipRecord))
        session.commit()

    with (
        pytest.raises(CutoverRejectedError) as raised,
        unit_of_work_factory() as uow,
    ):
        uow.annotation_imports.publish(job.job_id, actor_id="curator")

    assert [issue.line_number for issue in raised.value.report.issues] == [4]
    with unit_of_work_factory() as uow:
        mode = uow.annotation_imports.group_mode(GROUP)
    assert mode is AnnotationManagementMode.GPAD_IMPORTED
    assert annotations_of(session_factory) == []
