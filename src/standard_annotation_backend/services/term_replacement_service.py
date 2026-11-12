"""Queue and run term replacement jobs.

These jobs replace obsolete terms and alternative IDs in active annotations.
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
)
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    ReferenceDataUnavailableError,
)
from standard_annotation_backend.domain.term_replacement import (
    TermReplacementResult,
    replaced_annotation,
    term_replacement_parameters,
    term_replacement_scope,
)
from standard_annotation_backend.persistence.locks import AdvisoryTryLock, LockNamespace
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.sources import RefreshSources
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

    The trigger's transaction holds a lock on job creation until it ends, so two
    triggers committing at once share one job. A running job is never joined: it
    may have read its facts before the change that triggered this call. The
    caller commits, and dispatches the job only after committing.

    Args:
        unit_of_work: The trigger's transaction.
        group_key: The group to cover, or `None` for all groups.
        requested_by: The triggering job's requester, recorded as this job's.

    Returns:
        The queued job, new or joined.
    """
    unit_of_work.jobs.lock_term_replacement_starts()
    parameters = term_replacement_parameters(group_key)
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

    Each changed annotation is written through the same prepare, validate, and
    update steps as a client update, in its own transaction, so a replacement is
    made only when the result has no semantic issues.

    Args:
        unit_of_work_factory: Opens the transactions for reading and updating.
        sources: Configured sources, which name the ontologies and their prefixes.
        try_lock: Takes the job's lock and the scope's lock.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        try_lock: AdvisoryTryLock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._try_lock = try_lock
        self._jobs = JobService(unit_of_work_factory)
        self._validator = SemanticValidator(sources)
        self._ontologies = {
            key: sources.ontology_definition(key).identifier_prefixes
            for key in sources.keys(RefreshKindName.ONTOLOGY)
        }
        self._ontology_of = {
            prefix: key
            for key, prefixes in self._ontologies.items()
            for prefix in prefixes
        }

    def run(self, job_id: UUID) -> None:
        """Run one term replacement job to completion or failure.

        A job that another process is running, or that has finished, is left
        alone. A job with invalid parameters fails with `invalid_parameters`, and
        one that finds reference data missing fails with
        `reference_data_unavailable`.

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
                group_key = term_replacement_scope(job.parameters)
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
                        annotation, facts.terms, self._ontology_of
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
        """Write one replacement if it passes validation; return what happened.

        Returns:
            `replaced`, `issues`, or `stale`.
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
