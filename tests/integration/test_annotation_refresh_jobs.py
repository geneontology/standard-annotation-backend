"""Verify GPAD refresh and cutover jobs run through the shared refresh runner."""

import gzip
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from annotation_refresh_helpers import gpad_bytes, gpad_row, seed_group_import
from refresh_helpers import TEST_SOURCES, FakeFetchers, build_runner
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationStagingRecord,
    AuditEventRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.annotation import AnnotationRefreshBusyError
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)
from standard_annotation_backend.services.job_service import JobService

SOURCE = "mgi-gpad"
GOOD = gpad_bytes(gpad_row("UniProtKB:P12345"))


@pytest.fixture
def jobs(unit_of_work_factory: UnitOfWorkFactory) -> JobService:
    """Provide the real job service."""
    return JobService(unit_of_work_factory)


@pytest.fixture
def runner_for(
    database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory
) -> Callable[..., RefreshRunner]:
    """Return a function that builds a refresh runner using given fake fetchers."""

    def build(fetchers: FakeFetchers, **kwargs: Any) -> RefreshRunner:
        return build_runner(database_engine, unit_of_work_factory, fetchers, **kwargs)

    return build


@pytest.fixture(autouse=True)
def subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Make the GPAD test subject an active entity."""
    seed_active_subjects("UniProtKB:P12345")


def _job(jobs: JobService, job_type: JobType = JobType.ANNOTATION_REFRESH) -> UUID:
    return jobs.create(
        job_type=job_type, requested_by="curator", parameters={"source_key": SOURCE}
    ).job_id


def _group_annotations(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AnnotationRecord)
                .where(AnnotationRecord.owning_group_id == "MGI")
            )
            or 0
        )


def test_refresh_job_publishes_and_records_counts(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
) -> None:
    """A GPAD refresh job publishes the file and records its counts and result."""
    job_id = _job(jobs)

    runner_for(FakeFetchers({SOURCE: gzip.compress(GOOD)})).run(job_id)

    job = jobs.find(job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == {
        "phase": "completed",
        "data_rows": 1,
        "annotations_published": 1,
        "records_rejected": 0,
        "annotations_deleted": 0,
    }
    assert job.result is not None and job.result["mode"] == "gpad_imported"
    assert _group_annotations(session_factory) == 1


def test_unchanged_file_is_skipped_and_cutover_never_skips(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
) -> None:
    """A refresh skips a file it already imported into an unchanged group.

    A cutover imports the same file anyway.
    """
    fetchers = FakeFetchers({SOURCE: GOOD})
    first = _job(jobs)
    runner_for(fetchers).run(first)

    second = _job(jobs)
    runner_for(fetchers).run(second)
    cutover = _job(jobs, JobType.ANNOTATION_CUTOVER)
    runner_for(fetchers).run(cutover)

    first_result = jobs.find(first).result
    cutover_result = jobs.find(cutover).result
    assert first_result is not None and cutover_result is not None
    assert jobs.find(second).progress["unchanged"] is True
    assert jobs.find(second).result == {
        "source_key": SOURCE,
        "group_key": "MGI",
        "import_job_id": str(first),
        "source_checksum": first_result["source_checksum"],
    }
    assert cutover_result["mode"] == "sab_managed"
    assert _group_annotations(session_factory) == 1


@pytest.mark.parametrize(
    ("content", "job_type", "failure_code"),
    [
        (
            gpad_bytes(gpad_row("UniProtKB:UNKNOWN")),
            JobType.ANNOTATION_REFRESH,
            "no_valid_annotations",
        ),
        (
            gpad_bytes(gpad_row("UniProtKB:UNKNOWN")),
            JobType.ANNOTATION_CUTOVER,
            "no_valid_annotations",
        ),
        (
            gpad_bytes(gpad_row("UniProtKB:P12345"), "bad"),
            JobType.ANNOTATION_CUTOVER,
            "cutover_rejected",
        ),
        (
            gpad_bytes(gpad_row("UniProtKB:P12345"), gpad_row("UniProtKB:UNKNOWN")),
            JobType.ANNOTATION_CUTOVER,
            "cutover_rejected",
        ),
        (b"not gpad\n", JobType.ANNOTATION_REFRESH, "header"),
        (b"\x1f\x8bbroken", JobType.ANNOTATION_REFRESH, "invalid_gzip"),
    ],
    ids=[
        "refresh-no-valid",
        "cutover-no-valid",
        "cutover-unreadable-row",
        "cutover-unknown-subject",
        "header",
        "invalid-gzip",
    ],
)
def test_terminal_failures_keep_the_group_and_clean_staging(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    content: bytes,
    job_type: JobType,
    failure_code: str,
) -> None:
    """A failed job records its failure code and details and keeps the group's data.

    The group stays `gpad_imported`, and no staged annotations are left behind.
    """
    existing = gpad_bytes(gpad_row("UniProtKB:P12345", reference="PMID:7"))
    runner_for(FakeFetchers({SOURCE: existing})).run(_job(jobs))
    job_id = _job(jobs, job_type)

    runner_for(FakeFetchers({SOURCE: content})).run(job_id)

    job = jobs.find(job_id)
    assert job.status is JobStatus.FAILED
    assert job.error == "Annotation refresh failed"
    assert job.progress["failure_code"] == failure_code
    if failure_code in {"no_valid_annotations", "cutover_rejected"}:
        details = job.progress["failure_details"]
        assert isinstance(details, dict) and details["issue_count"] == 1
    assert _group_annotations(session_factory) == 1
    assert AnnotationRefreshService(unit_of_work_factory).group_mode("MGI") is (
        AnnotationManagementMode.GPAD_IMPORTED
    )
    # Leftover staging has no effect any client can see; it only occupies
    # storage. Counting staging rows is the only way to check that the failure
    # removed it.
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AnnotationStagingRecord))
            == 0
        )


def test_job_for_a_sab_managed_group_fails_with_group_sab_managed(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
) -> None:
    """A job for a group that is already SAB-managed fails with `group_sab_managed`."""
    seed_group_import(
        session_factory,
        group_key="MGI",
        source_key=SOURCE,
        mode=AnnotationManagementMode.SAB_MANAGED,
    )
    job_id = _job(jobs)

    runner_for(FakeFetchers({SOURCE: GOOD})).run(job_id)

    assert jobs.find(job_id).progress["failure_code"] == "group_sab_managed"


def test_redelivery_after_publication_recovers_without_fetching(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A job that published before its worker stopped succeeds without refetching.

    It reports the published result, and delivering the finished job again
    changes nothing. The publication is audited once.
    """
    job_id = _job(jobs)
    jobs.start(job_id)
    service = AnnotationRefreshService(unit_of_work_factory)
    service.stage(
        job_id=job_id,
        source_key=SOURCE,
        group_key="MGI",
        is_cutover=False,
        provenance=FakeFetchers({SOURCE: GOOD})
        .fetch(SOURCE, TEST_SOURCES.source(RefreshKindName.ANNOTATION, SOURCE))
        .provenance,
        text=GOOD.decode(),
    )
    service.publish(job_id=job_id, actor_id="curator")
    fetchers = FakeFetchers({})

    runner_for(fetchers).run(job_id)
    recovered = jobs.find(job_id)
    runner_for(fetchers).run(job_id)

    assert recovered.status is JobStatus.SUCCEEDED
    assert recovered.result is not None
    assert (recovered.result["annotations_published"], recovered.result["mode"]) == (
        1,
        "gpad_imported",
    )
    assert jobs.find(job_id) == recovered
    assert fetchers.calls == []
    assert _group_annotations(session_factory) == 1
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(
                    AuditEventRecord.action == AuditAction.ANNOTATION_REFRESH_PUBLISHED
                )
            )
            == 1
        )


def test_busy_group_lock_leaves_the_job_running_for_a_retry(
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    database_engine: Engine,
) -> None:
    """While another worker holds a group's lock, that group's job waits for a retry.

    The run raises a retryable error and leaves the job running, while a job for
    another group runs to completion. A later run of the waiting job succeeds.
    """
    job_id = _job(jobs)
    other_group = jobs.create(
        job_type=JobType.ANNOTATION_REFRESH,
        requested_by="curator",
        parameters={"source_key": "rgd-gpad"},
    ).job_id

    with try_advisory_lock(
        database_engine, LockNamespace.ANNOTATION_GROUP, "MGI"
    ) as acquired:
        assert acquired
        with pytest.raises(AnnotationRefreshBusyError):
            runner_for(FakeFetchers({SOURCE: GOOD})).run(job_id)
        runner_for(FakeFetchers({"rgd-gpad": GOOD})).run(other_group)

        assert jobs.find(job_id).status is JobStatus.RUNNING
        assert jobs.find(other_group).status is JobStatus.SUCCEEDED

    runner_for(FakeFetchers({SOURCE: GOOD})).run(job_id)
    assert jobs.find(job_id).status is JobStatus.SUCCEEDED
