"""Verify that activations and publications queue and dispatch replacement jobs."""

from collections.abc import Callable

from annotation_refresh_helpers import gpad_bytes, gpad_row
from refresh_helpers import (
    TEST_SOURCES,
    OboTerm,
    go_document,
    ignore_progress,
    obo,
    source_document,
    start_job,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)


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
    service = OntologyRefreshService(
        unit_of_work_factory,
        TEST_SOURCES,
        bind_try_lock(database_engine),
        None,
        dispatch_replacement=dispatched.append,
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")

    service.apply(job, go_document(obo(OboTerm("GO:0000001"))), ignore_progress)

    (queued,) = _replacement_jobs(session_factory)
    assert (queued.status, queued.parameters, queued.requested_by) == (
        JobStatus.QUEUED,
        {},
        job.requested_by,
    )
    assert [item.job_id for item in dispatched] == [queued.job_id]


def test_a_failed_dispatch_leaves_the_job_queued_for_the_next_trigger(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A dispatch failure does not fail the refresh, and the job stays queued.

    The next activation joins that queued job and dispatches it again.
    """

    def broken(_job: Job) -> None:
        raise RuntimeError("broker unavailable")

    lock = bind_try_lock(database_engine)
    failing = OntologyRefreshService(
        unit_of_work_factory, TEST_SOURCES, lock, None, dispatch_replacement=broken
    )
    first = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")

    outcome = failing.apply(
        first, go_document(obo(OboTerm("GO:0000001"))), ignore_progress
    )

    assert outcome.result["ontology_version_id"] is not None
    (queued,) = _replacement_jobs(session_factory)
    assert queued.status is JobStatus.QUEUED

    dispatched: list[Job] = []
    working = OntologyRefreshService(
        unit_of_work_factory,
        TEST_SOURCES,
        lock,
        None,
        dispatch_replacement=dispatched.append,
    )
    second = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    working.apply(
        second,
        go_document(obo(OboTerm("GO:0000001"), OboTerm("GO:0000002"))),
        ignore_progress,
    )

    assert [job.job_id for job in _replacement_jobs(session_factory)] == [queued.job_id]
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
    service = AnnotationRefreshService(
        unit_of_work_factory,
        TEST_SOURCES,
        bind_try_lock(database_engine),
        dispatch_replacement=dispatched.append,
    )
    content = gpad_bytes(gpad_row("UniProtKB:P12345"))
    first = start_job(unit_of_work_factory, JobType.ANNOTATION_REFRESH, "mgi-gpad")
    service.apply(first, source_document("mgi-gpad", content), ignore_progress)
    group_key = TEST_SOURCES.annotation_group("mgi-gpad")

    (queued,) = _replacement_jobs(session_factory)
    assert queued.parameters == {"group_key": group_key}
    assert [item.job_id for item in dispatched] == [queued.job_id]

    second = start_job(unit_of_work_factory, JobType.ANNOTATION_REFRESH, "mgi-gpad")
    outcome = service.apply(
        second, source_document("mgi-gpad", content), ignore_progress
    )

    assert outcome.unchanged
    assert len(_replacement_jobs(session_factory)) == 1
    assert len(dispatched) == 1
