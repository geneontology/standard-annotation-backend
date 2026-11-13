"""Queue and run term replacement jobs.

These jobs replace obsolete terms and alternative IDs in active annotations. An
ontology activation queues one for all groups, and an annotation import
publication queues one for the imported group.
"""

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationDeletedError,
    AnnotationNotFoundError,
    ChangeSource,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobNotFoundError,
    JobType,
    group_scope,
    group_scope_parameters,
)
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.domain.semantic_validation.facts import (
    ReferenceDataUnavailableError,
)
from standard_annotation_backend.domain.term_replacement import (
    TermReplacementResult,
    replaced_annotation,
)
from standard_annotation_backend.persistence.locks import AdvisoryTryLock, LockNamespace
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)
from standard_annotation_backend.services.semantic_validator import SemanticValidator


def queue_term_replacement(
    unit_of_work: SqlAlchemyUnitOfWork, *, group_key: str | None, requested_by: str
) -> Job:
    """Return the queued term replacement job for a scope, creating one if needed.

    A lock held until the caller's transaction ends lets only one caller at a
    time look for or create a job, so callers that commit at about the same time
    share one queued job. A job that is already running is never reused, because
    it may have read annotations and terms from before the caller's change; a new
    job is queued instead. The caller must commit, and only then send the job to
    a worker.

    Args:
        unit_of_work: The caller's transaction.
        group_key: The group to cover, or `None` for all groups.
        requested_by: The actor recorded as the requester of a new job, which
            is the requester of the caller's own job.

    Returns:
        The queued job, either new or already queued.
    """
    unit_of_work.jobs.lock_term_replacement_starts()
    parameters = group_scope_parameters(group_key)
    record = unit_of_work.jobs.find_queued(
        job_type=JobType.TERM_REPLACEMENT, parameters=parameters
    )
    if record is None:
        record = unit_of_work.jobs.create(
            job_type=JobType.TERM_REPLACEMENT,
            requested_by=requested_by,
            parameters=parameters,
            now=datetime.now(UTC),
        )
        AuditService(unit_of_work.audit).record_job_lifecycle(
            action=AuditAction.JOB_QUEUED, record=record
        )
    return job_from_record(record)


_BATCH_SIZE = 500
_FAILURE_MESSAGE = "Term replacement failed"


class TermReplacementBusyError(RuntimeError):
    """Report that another term replacement job for the same scope is running.

    The job stays queued; the Celery task retries it later.
    """

    def __init__(self) -> None:
        super().__init__("another term replacement job for this scope is running")


class TermReplacementService:
    """Run term replacement jobs.

    Each annotation that needs a replacement is updated in its own transaction,
    with the same locks, version check, and semantic validation as a client
    update. A replacement is saved only when the result has no semantic issues,
    and an annotation that changed after the job read it is skipped.

    Args:
        unit_of_work_factory: Opens the transactions for reading and updating.
        ontologies: The configured ontologies and the identifier prefixes each owns.
        try_lock: Takes a PostgreSQL advisory lock only if it is free. The
            service locks the job, so only one process runs it, and the job's
            scope, so only one job of a scope runs at a time.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        ontologies: OntologyCatalog,
        try_lock: AdvisoryTryLock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._try_lock = try_lock
        self._jobs = JobService(unit_of_work_factory)
        self._ontologies = ontologies
        self._validator = SemanticValidator(ontologies)

    def run(self, job_id: UUID) -> None:
        """Run one term replacement job to completion or failure.

        Nothing happens if another process is running the job or the job has
        finished. A job whose parameters are invalid fails with
        `invalid_parameters`, and one that finds required reference data missing
        fails with `reference_data_unavailable`. Each replacement commits on its
        own, so a job that stops early keeps the replacements it already made.

        Raises:
            TermReplacementBusyError: If another job for the same scope is
                running. The job stays queued.
        """
        with self._try_lock(LockNamespace.JOB, job_id) as acquired:
            if not acquired:
                return
            try:
                job = self._jobs.find(job_id)
            except JobNotFoundError:
                return
            if job.status in TERMINAL_JOB_STATUSES:
                return
            try:
                if job.job_type is not JobType.TERM_REPLACEMENT:
                    raise ValueError("not a term replacement job")
                group_key = group_scope(job.parameters)
            except ValueError:
                self._fail(job_id, RefreshFailureCode.INVALID_PARAMETERS)
                return
            with self._try_lock(LockNamespace.TERM_REPLACEMENT, group_key) as free:
                if not free:
                    raise TermReplacementBusyError
                job = self._jobs.start(job_id)
                try:
                    result = self._replace(job, group_key)
                except ReferenceDataUnavailableError:
                    self._fail(job_id, RefreshFailureCode.REFERENCE_DATA_UNAVAILABLE)
                    return
                self._jobs.update_progress(
                    job_id, progress={**result.to_progress(), "phase": "completed"}
                )
                self._jobs.succeed(job_id, result=result.to_job_result())

    def _replace(self, job: Job, group_key: str | None) -> TermReplacementResult:
        """Replace terms in every active annotation of the scope, batch by batch."""
        counts: Counter[str] = Counter()
        snapshot_ids: dict[str, set[UUID]] = {}
        after: UUID | None = None
        # Resolved before the first batch, so a scope with no annotations still
        # fails when reference data is missing.
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.validation_facts.active_snapshots(self._ontologies)
        while True:
            with self._unit_of_work_factory() as unit_of_work:
                records = unit_of_work.annotations.list_active_after(
                    after, owning_group_id=group_key, limit=_BATCH_SIZE
                )
                batch = [
                    (
                        record.annotation_id,
                        record.current_version,
                        record.annotation_data,
                    )
                    for record in records
                ]
                if not batch:
                    break
                after = batch[-1][0]
                readable: list[tuple[UUID, int, Annotation]] = []
                for annotation_id, version, data in batch:
                    counts["scanned"] += 1
                    try:
                        readable.append(
                            (annotation_id, version, Annotation.model_validate(data))
                        )
                    except ValidationError:
                        counts["issues"] += 1
                facts = unit_of_work.validation_facts.replacement_facts(
                    [annotation for _, _, annotation in readable], self._ontologies
                )
            for key, snapshot_id in facts.snapshot_ids.items():
                snapshot_ids.setdefault(key, set()).add(snapshot_id)
            for annotation_id, version, annotation in readable:
                try:
                    replaced = replaced_annotation(
                        annotation, facts.terms, self._ontologies
                    )
                except ValidationError:
                    # A replacement target the schema rejects, such as a
                    # malformed `replaced_by`, cannot be written.
                    counts["issues"] += 1
                    continue
                if replaced is not None:
                    counts[
                        self._replace_one(job, annotation_id, version, replaced)
                    ] += 1
            self._jobs.update_progress(
                job.job_id,
                progress={
                    "phase": "replacing",
                    **_result(group_key, counts, {}).to_progress(),
                },
            )
        return _result(group_key, counts, snapshot_ids)

    def _replace_one(
        self, job: Job, annotation_id: UUID, version: int, replaced: Annotation
    ) -> str:
        """Save one replacement if it passes validation, and return the outcome.

        Returns:
            `replaced` if a new version was saved, `issues` if the result has
            semantic issues, or `stale` if the annotation changed or was deleted
            after the job read it.
        """
        with self._unit_of_work_factory() as unit_of_work:
            try:
                prepared = unit_of_work.annotations.prepare_update(
                    annotation_id, replaced, expected_version=version
                )
            except (
                StaleAnnotationVersionError,
                AnnotationDeletedError,
                AnnotationNotFoundError,
            ):
                return "stale"
            if self._validator.issues(
                unit_of_work, replaced, prepared.duplicate_peer_ids
            ):
                return "issues"
            updated = unit_of_work.annotations.update(
                prepared,
                actor_id=job.requested_by,
                change_source=ChangeSource.TERM_REPLACEMENT,
            )
            AuditService(unit_of_work.audit).record_term_replacement(
                actor_id=job.requested_by,
                job_id=job.job_id,
                annotation_id=annotation_id,
                annotation_version=updated.current_version,
            )
            unit_of_work.commit()
            return "replaced"

    def _fail(self, job_id: UUID, code: RefreshFailureCode) -> None:
        self._jobs.fail_refresh(job_id, error=_FAILURE_MESSAGE, failure_code=code)


def _result(
    group_key: str | None,
    counts: Counter[str],
    snapshot_ids: dict[str, set[UUID]],
) -> TermReplacementResult:
    """Build a job result from the running counts."""
    return TermReplacementResult(
        group_key=group_key,
        annotation_scan_count=counts["scanned"],
        annotation_replaced_count=counts["replaced"],
        annotation_issue_skip_count=counts["issues"],
        annotation_stale_skip_count=counts["stale"],
        snapshot_ids={
            key: tuple(sorted(ids)) for key, ids in sorted(snapshot_ids.items())
        },
    )
