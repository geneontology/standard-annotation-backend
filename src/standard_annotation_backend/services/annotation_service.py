"""Provide annotation operations shared by HTTP and other entry points."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validate_annotation,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationNotFoundError,
    AnnotationSearchFilters,
    AuditRepository,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditAction, AuditService


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identify the person or process making a request.

    Attributes:
        actor_id: Stable identifier recorded in version history and audit events.
    """

    actor_id: str


@dataclass(frozen=True, slots=True)
class CurrentAnnotation:
    """Describe the latest active state of an annotation.

    Attributes:
        annotation_id: Unique identifier for the annotation across all versions.
        version: Number of the latest saved version.
        owning_group_id: Identifier of the group responsible for the annotation.
        record_origin: How the annotation entered the system.
        source_import_job_id: Import job that created the annotation, if any.
        created_at: Time when the annotation was first created.
        updated_at: Time when the current version was saved.
        annotation: Validated Standard Annotation data.
    """

    annotation_id: UUID
    version: int
    owning_group_id: str
    record_origin: str
    source_import_job_id: UUID | None
    created_at: datetime
    updated_at: datetime
    annotation: Annotation


@dataclass(frozen=True, slots=True)
class AnnotationVersion:
    """Describe one saved state in an annotation's history.

    Attributes:
        annotation_id: Identifier shared by every version of the annotation.
        version: Number of this saved version.
        is_deleted: Whether this version records the annotation's deletion.
        actor_id: Identifier for the person or process that made the change.
        change_source: Workflow through which the change was made.
        created_at: Time when this version was saved.
        annotation: Validated annotation data saved in this version.
    """

    annotation_id: UUID
    version: int
    is_deleted: bool
    actor_id: str
    change_source: str
    created_at: datetime
    annotation: Annotation


@dataclass(frozen=True, slots=True)
class ResultPage[T]:
    """Hold one requested portion of a larger result set.

    Attributes:
        items: Results included in this page.
        total: Number of results across all pages.
        limit: Maximum number of results requested for this page.
        offset: Number of matching results skipped before this page.
    """

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int


class InvalidAnnotationPayloadError(ValueError):
    """Report annotation data that does not satisfy the annotation schema.

    Attributes:
        errors: Validation problems found in the submitted annotation data.
    """

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        self.errors = errors
        super().__init__("annotation payload is invalid")


class EmptyAnnotationPatchError(ValueError):
    """Report an update request that does not supply any fields to replace."""


class AnnotationHistoryNotFoundError(LookupError):
    """Report an annotation or saved version that does not exist.

    Attributes:
        annotation_id: Identifier of the requested annotation.
        version: Requested version number, or `None` for the complete history.
    """

    def __init__(self, annotation_id: UUID, version: int | None = None) -> None:
        self.annotation_id = annotation_id
        self.version = version
        if version is None:
            message = f"annotation history {annotation_id} was not found"
        else:
            message = f"annotation {annotation_id} has no version {version}"
        super().__init__(message)


class AnnotationService:
    """Validate requests and complete each annotation operation atomically.

    Args:
        unit_of_work_factory: Callable that opens a transaction containing the
            annotation repositories used by each operation.
    """

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def create(
        self,
        *,
        payload: object,
        owning_group_id: str,
        context: RequestContext,
    ) -> CurrentAnnotation:
        """Validate and create an annotation submitted through the API.

        Args:
            payload: Untrusted annotation data to validate.
            owning_group_id: Identifier of the group responsible for the annotation.
            context: Identity information to record in version history.

        Returns:
            The newly created annotation at version 1.

        Raises:
            InvalidAnnotationPayloadError: If the annotation data is invalid.
            DuplicateAnnotationError: If an equivalent active annotation exists.
        """
        annotation = _validated_annotation(payload)
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.annotations.create_direct(
                annotation=annotation,
                actor_id=context.actor_id,
                owning_group_id=owning_group_id,
            )
            result = _current_annotation(record)
            _record_annotation_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_CREATED,
                context=context,
                annotation=result,
            )
            unit_of_work.commit()
        return result

    def get(self, annotation_id: UUID) -> CurrentAnnotation:
        """Return the latest state of an active annotation.

        Args:
            annotation_id: Identifier of the annotation to retrieve.

        Returns:
            The annotation's current active state.

        Raises:
            AnnotationNotFoundError: If the annotation is missing or deleted.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.annotations.get(annotation_id)
            if record is None:
                raise AnnotationNotFoundError(annotation_id)
            result = _current_annotation(record)
        return result

    def patch(
        self,
        annotation_id: UUID,
        *,
        changes: dict[str, object],
        expected_version: int,
        context: RequestContext,
    ) -> CurrentAnnotation:
        """Replace selected fields and save a new annotation version.

        Fields absent from `changes` keep their current values. Fields present in
        `changes` replace the corresponding top-level values completely.

        Args:
            annotation_id: Identifier of the annotation to update.
            changes: Top-level annotation fields and their replacement values.
            expected_version: Version that must still be current when saving.
            context: Identity information to record in version history.

        Returns:
            The updated annotation with its new version number.

        Raises:
            EmptyAnnotationPatchError: If `changes` contains no fields.
            AnnotationNotFoundError: If the annotation is missing or deleted.
            AnnotationDeletedError: If it is deleted after the initial read.
            InvalidAnnotationPayloadError: If the resulting annotation is invalid.
            DuplicateAnnotationError: If the change creates a new duplicate.
            StaleAnnotationVersionError: If `expected_version` is no longer current.
        """
        if not changes:
            raise EmptyAnnotationPatchError

        with self._unit_of_work_factory() as unit_of_work:
            current = unit_of_work.annotations.get(annotation_id)
            if current is None:
                raise AnnotationNotFoundError(annotation_id)
            if current.current_version != expected_version:
                raise StaleAnnotationVersionError(
                    annotation_id,
                    expected_version,
                    current.current_version,
                )

            merged = current.annotation_data.copy()
            merged.update(changes)
            annotation = _validated_annotation(merged)
            updated = unit_of_work.annotations.update_direct(
                annotation_id,
                annotation,
                expected_version=expected_version,
                actor_id=context.actor_id,
            )
            result = _current_annotation(updated)
            _record_annotation_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_UPDATED,
                context=context,
                annotation=result,
            )
            unit_of_work.commit()
        return result

    def delete(
        self,
        annotation_id: UUID,
        *,
        expected_version: int,
        context: RequestContext,
    ) -> None:
        """Mark an annotation as deleted while preserving its version history.

        Args:
            annotation_id: Identifier of the annotation to delete.
            expected_version: Version that must still be current when deleting.
            context: Identity information to record in version history.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            StaleAnnotationVersionError: If `expected_version` is no longer current.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.annotations.soft_delete_direct(
                annotation_id,
                expected_version=expected_version,
                actor_id=context.actor_id,
            )
            _record_annotation_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_DELETED,
                context=context,
                annotation=_current_annotation(record),
            )
            unit_of_work.commit()

    def list(
        self,
        *,
        db_object_id: str | None = None,
        negation: bool | None = None,
        relation: str | None = None,
        ontology_class_id: str | None = None,
        evidence_type: str | None = None,
        annotation_date: date | None = None,
        assigned_by: str | None = None,
        references: tuple[str, ...] = (),
        with_or_from: tuple[str, ...] = (),
        interacting_taxon_id: tuple[str, ...] = (),
        limit: int = 50,
        offset: int = 0,
    ) -> ResultPage[CurrentAnnotation]:
        """Find active annotations whose stored fields match every supplied filter.

        Repeating a list-valued filter requires an annotation to contain every
        supplied value. Results use a stable order so offset pagination is repeatable.

        Args:
            db_object_id: Database object identifier to match.
            negation: Negation value to match.
            relation: Relation identifier to match.
            ontology_class_id: Ontology class identifier to match.
            evidence_type: Evidence type identifier to match.
            annotation_date: Annotation date to match.
            assigned_by: Assigning organization to match.
            references: Reference identifiers that must all be present.
            with_or_from: Supporting identifiers that must all be present.
            interacting_taxon_id: Taxon identifiers that must all be present.
            limit: Maximum number of annotations to return.
            offset: Number of matching annotations to skip.

        Returns:
            The requested annotations and pagination information.
        """
        filters = AnnotationSearchFilters(
            db_object_id=db_object_id,
            negation=negation,
            relation=relation,
            ontology_class_id=ontology_class_id,
            evidence_type=evidence_type,
            annotation_date=annotation_date,
            assigned_by=assigned_by,
            references=references,
            with_or_from=with_or_from,
            interacting_taxon_id=interacting_taxon_id,
        )
        with self._unit_of_work_factory() as unit_of_work:
            page = unit_of_work.annotations.list_active(
                filters,
                limit=limit,
                offset=offset,
            )
            result = ResultPage(
                items=tuple(_current_annotation(record) for record in page.items),
                total=page.total,
                limit=limit,
                offset=offset,
            )
        return result

    def list_versions(
        self,
        annotation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> ResultPage[AnnotationVersion]:
        """Return saved versions of an annotation from oldest to newest.

        Deleted annotations remain available through this history operation.

        Args:
            annotation_id: Identifier of the annotation whose history is requested.
            limit: Maximum number of versions to return.
            offset: Number of versions to skip.

        Returns:
            The requested versions and pagination information.

        Raises:
            AnnotationHistoryNotFoundError: If the annotation does not exist.
        """
        with self._unit_of_work_factory() as unit_of_work:
            if not unit_of_work.annotations.annotation_exists(
                annotation_id,
                include_deleted=True,
            ):
                raise AnnotationHistoryNotFoundError(annotation_id)
            page = unit_of_work.annotations.list_versions_page(
                annotation_id,
                limit=limit,
                offset=offset,
            )
            result = ResultPage(
                items=tuple(_annotation_version(record) for record in page.items),
                total=page.total,
                limit=limit,
                offset=offset,
            )
        return result

    def get_version(
        self,
        annotation_id: UUID,
        version: int,
    ) -> AnnotationVersion:
        """Return one previously saved state of an annotation.

        Args:
            annotation_id: Identifier of the annotation to inspect.
            version: Positive version number to retrieve.

        Returns:
            The annotation data and change information saved for that version.

        Raises:
            AnnotationHistoryNotFoundError: If the annotation or version does not exist.
        """
        with self._unit_of_work_factory() as unit_of_work:
            if not unit_of_work.annotations.annotation_exists(
                annotation_id,
                include_deleted=True,
            ):
                raise AnnotationHistoryNotFoundError(annotation_id)
            record = unit_of_work.annotations.get_version(annotation_id, version)
            if record is None:
                raise AnnotationHistoryNotFoundError(annotation_id, version)
            result = _annotation_version(record)
        return result


def _record_annotation_audit(
    repository: AuditRepository,
    *,
    action: AuditAction,
    context: RequestContext,
    annotation: CurrentAnnotation,
) -> None:
    AuditService(repository).record_annotation_mutation(
        action=action,
        actor_id=context.actor_id,
        annotation_id=annotation.annotation_id,
        annotation_version=annotation.version,
    )


def _validated_annotation(payload: object) -> Annotation:
    validation = validate_annotation(payload)
    if validation.errors:
        raise InvalidAnnotationPayloadError(validation.errors)
    assert validation.annotation is not None
    return validation.annotation


def _current_annotation(record: AnnotationRecord) -> CurrentAnnotation:
    return CurrentAnnotation(
        annotation_id=record.annotation_id,
        version=record.current_version,
        owning_group_id=record.owning_group_id,
        record_origin=record.record_origin,
        source_import_job_id=record.source_import_job_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        annotation=Annotation.model_validate(record.annotation_data),
    )


def _annotation_version(record: AnnotationVersionRecord) -> AnnotationVersion:
    return AnnotationVersion(
        annotation_id=record.annotation_id,
        version=record.version,
        is_deleted=record.is_deleted,
        actor_id=record.actor_id,
        change_source=record.change_source,
        created_at=record.created_at,
        annotation=Annotation.model_validate(record.annotation_data),
    )
