"""Verify that activations and publications queue and dispatch replacement jobs."""

from collections.abc import Callable

from annotation_refresh_helpers import gpad_bytes, gpad_row
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    OboTerm,
    build_runner,
    obo,
)
from seeding import create_job
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job, JobService


def _replacement_jobs(session_factory: sessionmaker[Session]) -> list[JobRecord]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(JobRecord).where(JobRecord.job_type == JobType.TERM_REPLACEMENT)
            )
        )


def test_an_activation_queues_and_dispatches_a_job_for_all_groups(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """An activation queues one replacement job for all groups and dispatches it.

    The replacement job is requested by whoever requested the ontology refresh.
    """
    dispatched: list[Job] = []
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="curator",
        parameters={"source_key": "go"},
    )

    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": obo(OboTerm("GO:0000001"))}),
        dispatch_follow_up=dispatched.append,
    ).run(job.job_id)

    (queued,) = _replacement_jobs(session_factory)
    assert (queued.status, queued.parameters, queued.requested_by) == (
        JobStatus.QUEUED,
        {},
        job.requested_by,
    )
    assert [item.job_id for item in dispatched] == [queued.job_id]


def test_a_publication_queues_a_job_for_its_group_and_an_unchanged_file_does_not(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    seed_active_subjects: Callable[..., None],
) -> None:
    """A publication queues and dispatches a replacement job for its group only.

    Publishing replaces only that group's annotations, so only that group is
    rescanned. Applying the same file again publishes nothing and queues no job.
    """
    seed_active_subjects("UniProtKB:P12345")
    dispatched: list[Job] = []
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"mgi-gpad": gpad_bytes(gpad_row("UniProtKB:P12345"))}),
        dispatch_follow_up=dispatched.append,
    )

    def run_refresh() -> Job:
        job = create_job(
            unit_of_work_factory,
            job_type=JobType.ANNOTATION_REFRESH,
            requested_by="curator",
            parameters={"source_key": "mgi-gpad"},
        )
        runner.run(job.job_id)
        return JobService(unit_of_work_factory).find(job.job_id)

    run_refresh()

    (queued,) = _replacement_jobs(session_factory)
    assert queued.parameters == {"group_key": TEST_SOURCES.annotation_group("mgi-gpad")}
    assert [item.job_id for item in dispatched] == [queued.job_id]

    rerun = run_refresh()

    assert rerun.result is not None and rerun.result["unchanged"] is True
    assert len(_replacement_jobs(session_factory)) == 1
    assert len(dispatched) == 1
