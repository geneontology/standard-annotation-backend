"""Replace obsolete terms and alternative IDs in active annotations.

`TermReplacementScan` is the annotation scan for `term_replacement` jobs (see
`services/annotation_scan.py`). An ontology activation queues one for all groups,
and an annotation import publication queues one for the imported group.
"""

from collections import Counter
from functools import partial
from uuid import UUID

from pydantic import ValidationError

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationDeletedError,
    AnnotationNotFoundError,
    ChangeSource,
    InvalidAnnotationPayloadError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.domain.jobs import (
    JobType,
    ProgressReporter,
)
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    term_identifiers,
)
from standard_annotation_backend.domain.snapshots import ReferenceDataVersions
from standard_annotation_backend.domain.term_replacement import (
    TermReplacementResult,
    replaced_annotation,
    replacement_chain_terms,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.annotation_scan import (
    ReferenceDataLog,
    ScanOutcome,
)
from standard_annotation_backend.services.annotation_validator import (
    AnnotationValidator,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
)

_BATCH_SIZE = 500


class TermReplacementScan:
    """Replace obsolete terms and alternative IDs in active annotations.

    Each annotation that needs a replacement is updated in its own transaction,
    with the same locks, version check, and semantic validation as a client
    update. A replacement is saved only when the result has no semantic issues,
    and an annotation that changed after the job read it is skipped. A run that
    stops early keeps the replacements it already made. The result lists the
    reference data each batch and each validated replacement used.

    Args:
        unit_of_work_factory: Opens the transactions for reading and updating.
        ontologies: The configured ontologies and the identifier prefixes each owns.
    """

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, ontologies: OntologyCatalog
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._ontologies = ontologies
        self._validator = AnnotationValidator(ontologies)

    @property
    def job_type(self) -> JobType:
        """Return `term_replacement`."""
        return JobType.TERM_REPLACEMENT

    @property
    def failure_message(self) -> str:
        """Return "Term replacement failed"."""
        return "Term replacement failed"

    def prepare(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Do nothing: a replacement job keeps no rows of its own."""

    def execute(
        self, job: Job, group_key: str | None, report: ProgressReporter
    ) -> ScanOutcome:
        """Replace terms in every active annotation of `group_key`, or of all groups."""
        result = self._replace(job, group_key, report)
        return ScanOutcome(result=result.to_job_result(), counts=result.to_progress())

    def finalize(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Do nothing: every replacement has already committed."""

    def cleanup(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Do nothing: replacements made before a failure are kept."""

    def _replace(
        self, job: Job, group_key: str | None, report: ProgressReporter
    ) -> TermReplacementResult:
        """Replace terms in every active annotation of the scope, batch by batch."""
        counts: Counter[str] = Counter()
        reference_data = ReferenceDataLog()
        after: UUID | None = None
        # Resolved before the first batch, so a scope with no annotations still
        # fails when reference data is missing. Nothing is checked against this
        # set, so it is not recorded.
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.snapshots.resolve(self._ontologies)
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
                snapshots = unit_of_work.snapshots.resolve(self._ontologies)
                reference_data.record(unit_of_work, snapshots)
                terms = replacement_chain_terms(
                    {
                        occurrence.identifier
                        for _, _, annotation in readable
                        for occurrence in term_identifiers(annotation)
                    },
                    partial(
                        unit_of_work.validation_facts.terms,
                        ontologies=self._ontologies,
                        snapshots=snapshots,
                    ),
                )
            for annotation_id, version, annotation in readable:
                try:
                    replaced = replaced_annotation(annotation, terms, self._ontologies)
                except ValidationError:
                    # A replacement target the schema rejects, such as a
                    # malformed `replaced_by`, cannot be written.
                    counts["issues"] += 1
                    continue
                if replaced is not None:
                    counts[
                        self._replace_one(
                            job, annotation_id, version, replaced, reference_data
                        )
                    ] += 1
            report("replacing", _result(group_key, counts, ()).to_progress())
        return _result(group_key, counts, reference_data.versions())

    def _replace_one(
        self,
        job: Job,
        annotation_id: UUID,
        version: int,
        replaced: Annotation,
        reference_data: ReferenceDataLog,
    ) -> str:
        """Save one replacement if it passes validation, and return the outcome.

        The replacement is validated against the snapshots active when it is
        written. They are recorded in `reference_data` when validation ran,
        whether or not it passed; a stale annotation is never validated.

        Returns:
            `replaced` if a new version was saved, `issues` if the result has
            semantic issues, or `stale` if the annotation changed or was deleted
            after the job read it.
        """
        with self._unit_of_work_factory() as unit_of_work:
            snapshots = unit_of_work.snapshots.resolve(self._ontologies)
            try:
                validated = self._validator.prepare_update(
                    unit_of_work,
                    annotation_id,
                    replaced,
                    expected_version=version,
                    snapshots=snapshots,
                )
            except (
                StaleAnnotationVersionError,
                AnnotationDeletedError,
                AnnotationNotFoundError,
            ):
                return "stale"
            except InvalidAnnotationPayloadError:
                reference_data.record(unit_of_work, snapshots)
                return "issues"
            reference_data.record(unit_of_work, snapshots)
            updated = unit_of_work.annotations.update(
                validated,
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


def _result(
    group_key: str | None,
    counts: Counter[str],
    reference_data: tuple[ReferenceDataVersions, ...],
) -> TermReplacementResult:
    """Build a job result from the running counts."""
    return TermReplacementResult(
        group_key=group_key,
        annotation_scan_count=counts["scanned"],
        annotation_replaced_count=counts["replaced"],
        annotation_issue_skip_count=counts["issues"],
        annotation_stale_skip_count=counts["stale"],
        reference_data=reference_data,
    )
