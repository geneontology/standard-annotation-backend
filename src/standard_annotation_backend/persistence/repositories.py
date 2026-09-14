"""Read and write annotations and comments with SQLAlchemy sessions."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import Annotation, new_annotation_id
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
    acquire_signature_locks,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationOrigin,
    AnnotationRecord,
    AnnotationStatus,
    AnnotationVersionRecord,
)


@dataclass(frozen=True, slots=True)
class AnnotationSearchFilters:
    """Hold exact-match criteria for finding active annotations.

    Each scalar value must match exactly. Every value in a tuple must be present
    in the corresponding list-valued annotation field.

    Attributes:
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
    """

    db_object_id: str | None = None
    negation: bool | None = None
    relation: str | None = None
    ontology_class_id: str | None = None
    evidence_type: str | None = None
    annotation_date: date | None = None
    assigned_by: str | None = None
    references: tuple[str, ...] = ()
    with_or_from: tuple[str, ...] = ()
    interacting_taxon_id: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Hold selected results and the size of the complete result set.

    Attributes:
        items: Records included in the requested page.
        total: Number of records matching the query before pagination.
    """

    items: tuple[T, ...]
    total: int


class AnnotationNotFoundError(LookupError):
    """Raised when an annotation write targets an unknown identifier."""


class AnnotationDeletedError(RuntimeError):
    """Raised when an annotation write targets a soft-deleted annotation."""


class DuplicateAnnotationError(RuntimeError):
    """Report a requested change that would create an active duplicate.

    Attributes:
        peer_ids: Identifiers of active annotations equivalent to the proposed data.
    """

    def __init__(self, peer_ids: Iterable[UUID]) -> None:
        self.peer_ids = tuple(peer_ids)
        super().__init__("annotation conflicts with an active duplicate")


class StaleAnnotationVersionError(RuntimeError):
    """Report that an annotation changed after a client last read it.

    Attributes:
        annotation_id: Identifier of the annotation being changed.
        expected_version: Version supplied by the client.
        current_version: Version stored when the change was attempted.
    """

    def __init__(
        self,
        annotation_id: UUID,
        expected_version: int,
        current_version: int,
    ) -> None:
        self.annotation_id = annotation_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(f"annotation {annotation_id} is at version {current_version}")


class InvalidAnnotationProvenanceError(ValueError):
    """Raised when annotation provenance is unsupported or inconsistent."""


class CommentNotFoundError(LookupError):
    """Raised when a comment write targets a missing or deleted comment."""


class InvalidCommentError(ValueError):
    """Raised when a comment body is blank."""


class AnnotationRepository:
    """Read and write annotations in a caller-managed transaction.

    The repository flushes changes so database errors are raised promptly, but
    the caller remains responsible for committing or rolling back the session.

    Mutation methods such as `create`, `update`, and `soft_delete` support workflows
    whose callers supply their own workflow details. Their `_direct` counterparts
    handle changes submitted through the public API and enforce that workflow's
    additional rules, including duplicate prevention and expected-version checks.
    Both variants use shared private helpers for the underlying database writes so
    annotations and their version histories remain consistent.

    Args:
        session: Session used for every query and write.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> AnnotationRecord | None:
        """Get an annotation's current database record.

        Args:
            annotation_id: Identifier of the annotation to find.
            include_deleted: Whether a deleted annotation may be returned.

        Returns:
            The current record, or `None` when no visible record exists.
        """
        statement = select(AnnotationRecord).where(
            AnnotationRecord.annotation_id == annotation_id
        )
        if not include_deleted:
            statement = statement.where(
                AnnotationRecord.status == AnnotationStatus.ACTIVE.value
            )
        return self.session.scalar(statement)

    def list_versions(
        self,
        annotation_id: UUID,
    ) -> tuple[AnnotationVersionRecord, ...]:
        """List the saved versions of an annotation.

        Args:
            annotation_id: Identifier of the annotation whose history is needed.

        Returns:
            Saved versions ordered from oldest to newest.
        """
        statement = (
            select(AnnotationVersionRecord)
            .where(AnnotationVersionRecord.annotation_id == annotation_id)
            .order_by(AnnotationVersionRecord.version)
        )
        return tuple(self.session.scalars(statement))

    def annotation_exists(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = True,
    ) -> bool:
        """Return whether an annotation with the exact identifier exists.

        Args:
            annotation_id: Identifier to look up.
            include_deleted: Whether a deleted annotation counts as existing.

        Returns:
            `True` when a matching visible record exists; otherwise `False`.
        """
        statement = select(AnnotationRecord.annotation_id).where(
            AnnotationRecord.annotation_id == annotation_id
        )
        if not include_deleted:
            statement = statement.where(
                AnnotationRecord.status == AnnotationStatus.ACTIVE.value
            )
        return self.session.scalar(statement) is not None

    def list_versions_page(
        self,
        annotation_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> Page[AnnotationVersionRecord]:
        """List one page of an annotation's saved versions, oldest first.

        Args:
            annotation_id: Identifier of the annotation whose history is requested.
            limit: Maximum number of versions to return.
            offset: Number of versions to skip.

        Returns:
            The selected version records and the total number of saved versions.
        """
        statement = select(AnnotationVersionRecord).where(
            AnnotationVersionRecord.annotation_id == annotation_id
        )
        count_statement = select(func.count()).select_from(statement.subquery())
        total = self.session.scalar(count_statement)
        assert total is not None

        statement = statement.order_by(AnnotationVersionRecord.version)
        items = tuple(self.session.scalars(statement.limit(limit).offset(offset)))
        return Page(items=items, total=total)

    def get_version(
        self,
        annotation_id: UUID,
        version: int,
    ) -> AnnotationVersionRecord | None:
        """Get one saved annotation version.

        Args:
            annotation_id: Identifier shared by all versions of the annotation.
            version: Version number to retrieve.

        Returns:
            The saved version, or `None` when it does not exist.
        """
        return self.session.get(AnnotationVersionRecord, (annotation_id, version))

    def list_active(
        self,
        filters: AnnotationSearchFilters,
        *,
        limit: int,
        offset: int,
    ) -> Page[AnnotationRecord]:
        """List active annotations that match every supplied filter.

        Args:
            filters: Exact field values that matching annotations must contain.
            limit: Maximum number of annotations to return.
            offset: Number of matching annotations to skip.

        Returns:
            The selected annotation records and total number of matches.
        """
        statement = select(AnnotationRecord).where(
            AnnotationRecord.status == AnnotationStatus.ACTIVE.value
        )
        for field_name in (
            "db_object_id",
            "negation",
            "relation",
            "ontology_class_id",
            "evidence_type",
            "annotation_date",
            "assigned_by",
        ):
            value = getattr(filters, field_name)
            if value is not None:
                statement = statement.where(
                    getattr(AnnotationRecord, field_name) == value
                )
        for field_name in (
            "references",
            "with_or_from",
            "interacting_taxon_id",
        ):
            for field_value in getattr(filters, field_name):
                statement = statement.where(
                    exists(
                        select(1).where(
                            AnnotationMultivaluedFieldValueRecord.annotation_id
                            == AnnotationRecord.annotation_id,
                            AnnotationMultivaluedFieldValueRecord.field_name
                            == field_name,
                            AnnotationMultivaluedFieldValueRecord.field_value
                            == field_value,
                        )
                    )
                )

        count_statement = select(func.count()).select_from(
            statement.order_by(None).subquery()
        )
        total = self.session.scalar(count_statement)
        assert total is not None

        statement = statement.order_by(
            AnnotationRecord.created_at,
            AnnotationRecord.annotation_id,
        )
        items = tuple(self.session.scalars(statement.limit(limit).offset(offset)))
        return Page(items=items, total=total)

    def find_duplicate_peer_ids(
        self,
        duplicate_base_signature: str,
        canonical_references: Iterable[str],
        *,
        exclude_annotation_id: UUID | None = None,
    ) -> tuple[UUID, ...]:
        """Find active annotations that may duplicate a candidate annotation.

        Args:
            duplicate_base_signature: Signature for the candidate's normalized
                non-reference fields.
            canonical_references: Normalized references from the candidate.
            exclude_annotation_id: Annotation to leave out of the results, such
                as the annotation currently being updated.

        Returns:
            Matching annotation identifiers in stable order.
        """
        references = tuple(set(canonical_references))
        if not references:
            return ()

        statement = (
            select(AnnotationDuplicateReferenceRecord.annotation_id)
            .join(
                AnnotationRecord,
                AnnotationRecord.annotation_id
                == AnnotationDuplicateReferenceRecord.annotation_id,
            )
            .where(
                AnnotationRecord.status == AnnotationStatus.ACTIVE.value,
                AnnotationDuplicateReferenceRecord.duplicate_base_signature
                == duplicate_base_signature,
                AnnotationDuplicateReferenceRecord.canonical_reference.in_(references),
            )
            .distinct()
            .order_by(AnnotationDuplicateReferenceRecord.annotation_id)
        )
        if exclude_annotation_id is not None:
            statement = statement.where(
                AnnotationDuplicateReferenceRecord.annotation_id
                != exclude_annotation_id
            )
        return tuple(self.session.scalars(statement))

    def create(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        change_source: str,
        owning_group_id: str,
        record_origin: str | AnnotationOrigin,
        source_import_job_id: UUID | None = None,
        annotation_id: UUID | None = None,
    ) -> AnnotationRecord:
        """Store a new annotation and its first version.

        Args:
            annotation: Validated annotation to store.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.
            owning_group_id: Group responsible for the annotation.
            record_origin: Whether the annotation was created directly or
                imported.
            source_import_job_id: Import job that supplied the annotation.
            annotation_id: Identifier to use instead of generating one.

        Returns:
            The new current annotation record.

        Raises:
            InvalidAnnotationProvenanceError: If the origin and import job do not
                describe a valid direct or imported record.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        origin = (
            record_origin.value
            if isinstance(record_origin, AnnotationOrigin)
            else record_origin
        )
        self._validate_provenance(origin, source_import_job_id)
        persistence_data = prepare_annotation_for_persistence(annotation)

        acquire_global_annotation_write_lock(self.session)
        acquire_signature_locks(
            self.session,
            [persistence_data.duplicate_base_signature],
        )

        return self._insert_annotation(
            annotation=annotation,
            persistence_data=persistence_data,
            actor_id=actor_id,
            change_source=change_source,
            owning_group_id=owning_group_id,
            record_origin=origin,
            source_import_job_id=source_import_job_id,
            annotation_id=annotation_id or new_annotation_id(),
        )

    def create_direct(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        owning_group_id: str,
    ) -> AnnotationRecord:
        """Store an API-submitted annotation unless an equivalent one is active.

        Args:
            annotation: Validated annotation to store.
            actor_id: Identifier for the person or process creating the annotation.
            owning_group_id: Identifier of the responsible group.

        Returns:
            The newly stored annotation at version 1.

        Raises:
            DuplicateAnnotationError: If an equivalent active annotation exists.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        persistence_data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        acquire_signature_locks(
            self.session,
            [persistence_data.duplicate_base_signature],
        )
        peers = self.find_duplicate_peer_ids(
            persistence_data.duplicate_base_signature,
            persistence_data.canonical_references,
        )
        if peers:
            raise DuplicateAnnotationError(peers)
        return self._insert_annotation(
            annotation=annotation,
            persistence_data=persistence_data,
            actor_id=actor_id,
            change_source="api",
            owning_group_id=owning_group_id,
            record_origin=AnnotationOrigin.DIRECT.value,
            source_import_job_id=None,
            annotation_id=new_annotation_id(),
        )

    def _insert_annotation(
        self,
        *,
        annotation: Annotation,
        persistence_data: AnnotationPersistenceData,
        actor_id: str,
        change_source: str,
        owning_group_id: str,
        record_origin: str,
        source_import_job_id: UUID | None,
        annotation_id: UUID,
    ) -> AnnotationRecord:
        """Store a new annotation and its records used for history and search.

        The caller must prepare the annotation data, obtain the necessary database
        locks, and perform the policy checks required by its workflow. API creation,
        for example, checks for duplicates; import workflows may allow them.

        Args:
            annotation: Validated annotation to store.
            persistence_data: Annotation data prepared for database storage.
            actor_id: Identifier for the person or process creating the annotation.
            change_source: Workflow through which the annotation was created.
            owning_group_id: Identifier of the responsible group.
            record_origin: How the annotation entered the system.
            source_import_job_id: Import job that created the annotation, if any.
            annotation_id: Unique identifier assigned to the annotation.

        Returns:
            The newly stored annotation at version 1.
        """
        record = AnnotationRecord(
            annotation_id=annotation_id,
            annotation_data=persistence_data.annotation_data,
            current_version=1,
            status=AnnotationStatus.ACTIVE.value,
            deleted_at=None,
            owning_group_id=owning_group_id,
            record_origin=record_origin,
            source_import_job_id=source_import_job_id,
            duplicate_base_signature=persistence_data.duplicate_base_signature,
            db_object_id=persistence_data.db_object_id,
            negation=persistence_data.negation,
            relation=persistence_data.relation,
            ontology_class_id=persistence_data.ontology_class_id,
            evidence_type=persistence_data.evidence_type,
            annotation_date=persistence_data.annotation_date,
            assigned_by=persistence_data.assigned_by,
        )
        self.session.add(record)
        self.session.flush([record])
        self.session.add(
            AnnotationVersionRecord(
                annotation_id=annotation_id,
                version=1,
                annotation_data=persistence_data.annotation_data,
                is_deleted=False,
                actor_id=actor_id,
                change_source=change_source,
            )
        )
        self._add_derived_values(annotation_id, persistence_data)
        self.session.flush()
        return record

    def update(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Replace an annotation's current data and add a saved version.

        Args:
            annotation_id: Identifier of the annotation to update.
            annotation: Validated replacement annotation.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.

        Returns:
            The updated current annotation record.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        persistence_data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)

        acquire_signature_locks(
            self.session,
            [
                record.duplicate_base_signature,
                persistence_data.duplicate_base_signature,
            ],
        )

        return self._apply_update(
            record,
            persistence_data,
            actor_id=actor_id,
            change_source=change_source,
        )

    def update_direct(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        expected_version: int,
        actor_id: str,
    ) -> AnnotationRecord:
        """Update an API-submitted annotation when the client version is current.

        Args:
            annotation_id: Identifier of the annotation to update.
            annotation: Validated replacement annotation.
            expected_version: Version supplied by the client.
            actor_id: Identifier for the person or process making the change.

        Returns:
            The updated annotation with a newly saved version.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            DuplicateAnnotationError: If the change creates a new duplicate.
            StaleAnnotationVersionError: If `expected_version` is not current.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        candidate = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        current = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(current)
        before = prepare_annotation_for_persistence(
            Annotation.model_validate(current.annotation_data)
        )

        acquire_signature_locks(
            self.session,
            [before.duplicate_base_signature, candidate.duplicate_base_signature],
        )

        current = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(current)
        refreshed_before = prepare_annotation_for_persistence(
            Annotation.model_validate(current.annotation_data)
        )
        if refreshed_before != before:
            before = refreshed_before
        if current.current_version != expected_version:
            raise StaleAnnotationVersionError(
                annotation_id,
                expected_version,
                current.current_version,
            )

        before_peers = set(
            self.find_duplicate_peer_ids(
                before.duplicate_base_signature,
                before.canonical_references,
                exclude_annotation_id=annotation_id,
            )
        )
        after_peers = set(
            self.find_duplicate_peer_ids(
                candidate.duplicate_base_signature,
                candidate.canonical_references,
                exclude_annotation_id=annotation_id,
            )
        )
        introduced = tuple(sorted(after_peers - before_peers))
        if introduced:
            raise DuplicateAnnotationError(introduced)

        return self._apply_update(
            current,
            candidate,
            actor_id=actor_id,
            change_source="api",
        )

    def _apply_update(
        self,
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Save replacement data and rebuild the records used for searching.

        The caller must prepare the replacement data, obtain the necessary database
        locks, and perform the policy checks required by its workflow. API updates,
        for example, check the client version and prevent new duplicates.

        Args:
            record: Current database record to update.
            persistence_data: Replacement data prepared for database storage.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow through which the change was made.

        Returns:
            The updated annotation with a newly saved version.
        """
        next_version = record.current_version + 1
        now = datetime.now(UTC)
        self._update_current_record(record, persistence_data)
        record.current_version = next_version
        record.updated_at = now

        self.session.add(
            AnnotationVersionRecord(
                annotation_id=record.annotation_id,
                version=next_version,
                annotation_data=persistence_data.annotation_data,
                is_deleted=False,
                actor_id=actor_id,
                change_source=change_source,
                created_at=now,
            )
        )
        self._delete_derived_values(record.annotation_id)
        self._add_derived_values(record.annotation_id, persistence_data)
        self.session.flush()
        return record

    def soft_delete(
        self,
        annotation_id: UUID,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Mark an annotation as deleted and save that change as a new version.

        Args:
            annotation_id: Identifier of the annotation to delete.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.

        Returns:
            The annotation record in its deleted state.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
        """
        acquire_global_annotation_write_lock(self.session)
        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)

        acquire_signature_locks(self.session, [record.duplicate_base_signature])

        return self._apply_soft_delete(
            record,
            actor_id=actor_id,
            change_source=change_source,
        )

    def soft_delete_direct(
        self,
        annotation_id: UUID,
        *,
        expected_version: int,
        actor_id: str,
    ) -> AnnotationRecord:
        """Mark an API-submitted annotation as deleted when its version matches.

        The annotation's database record and saved versions remain available for
        history queries, but current-annotation queries no longer return it.

        Args:
            annotation_id: Identifier of the annotation to delete.
            expected_version: Version supplied by the client.
            actor_id: Identifier for the person or process making the change.

        Returns:
            The annotation record in its deleted state.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            StaleAnnotationVersionError: If `expected_version` is not current.
        """
        acquire_global_annotation_write_lock(self.session)
        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)
        acquire_signature_locks(self.session, [record.duplicate_base_signature])

        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)
        if record.current_version != expected_version:
            raise StaleAnnotationVersionError(
                annotation_id,
                expected_version,
                record.current_version,
            )

        return self._apply_soft_delete(
            record,
            actor_id=actor_id,
            change_source="api",
        )

    def _apply_soft_delete(
        self,
        record: AnnotationRecord,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Save a deletion version and remove records used for active searches.

        The caller must confirm that the record is active, obtain the necessary
        database locks, and perform any additional checks required by its workflow.
        API deletion, for example, checks the version supplied by the client.

        Args:
            record: Current database record to mark as deleted.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow through which the deletion was requested.

        Returns:
            The annotation record in its deleted state.
        """
        next_version = record.current_version + 1
        now = datetime.now(UTC)
        record.current_version = next_version
        record.status = AnnotationStatus.DELETED.value
        record.deleted_at = now
        record.updated_at = now
        self.session.add(
            AnnotationVersionRecord(
                annotation_id=record.annotation_id,
                version=next_version,
                annotation_data=record.annotation_data,
                is_deleted=True,
                actor_id=actor_id,
                change_source=change_source,
                created_at=now,
            )
        )
        self._delete_derived_values(record.annotation_id)
        self.session.flush()
        return record

    @staticmethod
    def _validate_provenance(
        record_origin: str,
        source_import_job_id: UUID | None,
    ) -> None:
        try:
            AnnotationOrigin(record_origin)
        except ValueError:
            raise InvalidAnnotationProvenanceError(
                f"{record_origin!r} is not a supported annotation record origin"
            ) from None

        if (
            record_origin == AnnotationOrigin.DIRECT.value
            and source_import_job_id is not None
        ):
            raise InvalidAnnotationProvenanceError(
                "direct annotations cannot have a source import job"
            )
        if (
            record_origin == AnnotationOrigin.IMPORT.value
            and source_import_job_id is None
        ):
            raise InvalidAnnotationProvenanceError(
                "imported annotations require a source import job"
            )

    def _get_annotation_for_change(self, annotation_id: UUID) -> AnnotationRecord:
        """Get the current annotation and prevent concurrent changes to it.

        Args:
            annotation_id: Identifier of the annotation being changed.

        Returns:
            The current annotation record.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
        """
        statement = (
            select(AnnotationRecord)
            .where(AnnotationRecord.annotation_id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        record = self.session.scalar(statement)
        if record is None:
            raise AnnotationNotFoundError(f"annotation {annotation_id} was not found")
        return record

    @staticmethod
    def _raise_if_deleted(record: AnnotationRecord) -> None:
        if record.status == AnnotationStatus.DELETED.value:
            raise AnnotationDeletedError(
                f"annotation {record.annotation_id} is already deleted"
            )

    @staticmethod
    def _update_current_record(
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
    ) -> None:
        record.annotation_data = persistence_data.annotation_data
        record.duplicate_base_signature = persistence_data.duplicate_base_signature
        record.db_object_id = persistence_data.db_object_id
        record.negation = persistence_data.negation
        record.relation = persistence_data.relation
        record.ontology_class_id = persistence_data.ontology_class_id
        record.evidence_type = persistence_data.evidence_type
        record.annotation_date = persistence_data.annotation_date
        record.assigned_by = persistence_data.assigned_by

    def _delete_derived_values(self, annotation_id: UUID) -> None:
        self.session.execute(
            delete(AnnotationMultivaluedFieldValueRecord).where(
                AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id
            )
        )
        self.session.execute(
            delete(AnnotationDuplicateReferenceRecord).where(
                AnnotationDuplicateReferenceRecord.annotation_id == annotation_id
            )
        )

    def _add_derived_values(
        self,
        annotation_id: UUID,
        persistence_data: AnnotationPersistenceData,
    ) -> None:
        self.session.add_all(
            AnnotationMultivaluedFieldValueRecord(
                annotation_id=annotation_id,
                field_name=value.field_name,
                field_value=value.field_value,
            )
            for value in persistence_data.multivalued_field_values
        )
        self.session.add_all(
            AnnotationDuplicateReferenceRecord(
                annotation_id=annotation_id,
                canonical_reference=reference,
                duplicate_base_signature=persistence_data.duplicate_base_signature,
            )
            for reference in persistence_data.canonical_references
        )


class AnnotationCommentRepository:
    """Read and write comments in a caller-managed transaction.

    Each comment stays attached to the annotation version that was current when
    the comment was created.

    Args:
        session: Session used for every query and write.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        comment_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> AnnotationCommentRecord | None:
        """Get a comment by identifier.

        Args:
            comment_id: Identifier of the comment to find.
            include_deleted: Whether a deleted comment may be returned.

        Returns:
            The comment, or `None` when no visible comment exists.
        """
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.comment_id == comment_id
        )
        if not include_deleted:
            statement = statement.where(AnnotationCommentRecord.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> tuple[AnnotationCommentRecord, ...]:
        """List comments attached to an annotation.

        Args:
            annotation_id: Identifier of the annotation to list comments for.
            include_deleted: Whether deleted comments should be included.

        Returns:
            Comments ordered by creation time and identifier.
        """
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.annotation_id == annotation_id
        )
        if not include_deleted:
            statement = statement.where(AnnotationCommentRecord.deleted_at.is_(None))
        statement = statement.order_by(
            AnnotationCommentRecord.created_at,
            AnnotationCommentRecord.comment_id,
        )
        return tuple(self.session.scalars(statement))

    def create(
        self,
        annotation_id: UUID,
        *,
        body: str,
        created_by: str,
        comment_id: UUID | None = None,
    ) -> AnnotationCommentRecord:
        """Create a comment on an annotation's current version.

        Args:
            annotation_id: Identifier of the annotation being discussed.
            body: Nonblank comment text.
            created_by: Identifier for the comment author.
            comment_id: Identifier to use instead of generating one.

        Returns:
            The new comment record.

        Raises:
            InvalidCommentError: If `body` is blank.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        annotation = self._get_active_annotation_for_comment(annotation_id)

        now = datetime.now(UTC)
        comment = AnnotationCommentRecord(
            comment_id=comment_id or uuid4(),
            annotation_id=annotation_id,
            annotation_version=annotation.current_version,
            body=body,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self.session.add(comment)
        self.session.flush([comment])
        return comment

    def edit(self, comment_id: UUID, *, body: str) -> AnnotationCommentRecord:
        """Change the text of an existing comment.

        Args:
            comment_id: Identifier of the comment to edit.
            body: New nonblank comment text.

        Returns:
            The updated comment record.

        Raises:
            InvalidCommentError: If `body` is blank.
            CommentNotFoundError: If the comment is missing or deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        comment = self._get_active_comment_for_change(comment_id)
        comment.body = body
        comment.updated_at = datetime.now(UTC)
        self.session.flush([comment])
        return comment

    def soft_delete(self, comment_id: UUID) -> AnnotationCommentRecord:
        """Mark a comment as deleted without removing its row.

        Args:
            comment_id: Identifier of the comment to delete.

        Returns:
            The comment record in its deleted state.

        Raises:
            CommentNotFoundError: If the comment is missing or already deleted.
        """
        acquire_global_annotation_write_lock(self.session)
        comment = self._get_active_comment_for_change(comment_id)
        now = datetime.now(UTC)
        comment.updated_at = now
        comment.deleted_at = now
        self.session.flush([comment])
        return comment

    def _get_active_annotation_for_comment(
        self,
        annotation_id: UUID,
    ) -> AnnotationRecord:
        """Get the active annotation version that a new comment will reference.

        Other transactions cannot change the annotation until this transaction ends.

        Args:
            annotation_id: Identifier of the annotation being discussed.

        Returns:
            The active annotation record with its current version protected.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        statement = (
            select(AnnotationRecord)
            .where(AnnotationRecord.annotation_id == annotation_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        annotation = self.session.scalar(statement)
        if annotation is None:
            raise AnnotationNotFoundError(f"annotation {annotation_id} was not found")
        if annotation.status == AnnotationStatus.DELETED.value:
            raise AnnotationDeletedError(
                f"annotation {annotation.annotation_id} is already deleted"
            )
        return annotation

    def _get_active_comment_for_change(
        self,
        comment_id: UUID,
    ) -> AnnotationCommentRecord:
        """Get an active comment and prevent concurrent changes to it.

        Args:
            comment_id: Identifier of the comment being changed.

        Returns:
            The active comment record.

        Raises:
            CommentNotFoundError: If the comment is missing or deleted.
        """
        statement = (
            select(AnnotationCommentRecord)
            .where(
                AnnotationCommentRecord.comment_id == comment_id,
                AnnotationCommentRecord.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        comment = self.session.scalar(statement)
        if comment is None:
            raise CommentNotFoundError(
                f"comment {comment_id} was not found or is deleted"
            )
        return comment

    @staticmethod
    def _validate_body(body: str) -> None:
        if not body.strip():
            raise InvalidCommentError("comment body must not be blank")
