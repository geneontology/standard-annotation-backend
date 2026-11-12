"""Read and write annotations in caller-managed transactions."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotation_search import (
    AnnotationFilter,
    OwnershipScope,
)
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationOrigin,
    AnnotationStatus,
    ChangeSource,
    StaleAnnotationVersionError,
    new_annotation_id,
)
from standard_annotation_backend.domain.ontology import GO_ONTOLOGY_KEY
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
    acquire_signature_locks,
)
from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    OntologyClosureRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.repositories.pagination import (
    Page,
    load_page,
)


def _closure_condition(term_id: str, predicate_id: str) -> ColumnElement[bool]:
    """Match annotations whose ontology class is the term or one of its descendants.

    Descendants are read from the closure (precomputed ancestor relationships)
    of the active GO ontology through the given predicate.

    Args:
        term_id: Ontology term whose descendants-or-self are matched.
        predicate_id: Loaded closure predicate relating descendants to the term.

    Returns:
        An SQL condition on the annotation's ontology class.
    """
    return exists(
        select(1)
        .select_from(OntologyClosureRecord)
        .join(
            OntologyMetadataRecord,
            OntologyMetadataRecord.version_id == OntologyClosureRecord.version_id,
        )
        .where(
            OntologyMetadataRecord.ontology_key == GO_ONTOLOGY_KEY,
            OntologyMetadataRecord.active.is_(True),
            OntologyClosureRecord.subject_term_id == AnnotationRecord.ontology_class_id,
            OntologyClosureRecord.object_term_id == term_id,
            OntologyClosureRecord.predicate_id == predicate_id,
        )
    )


def _multivalued_condition(field_name: str, field_value: str) -> ColumnElement[bool]:
    """Match annotations whose list-valued field contains a value.

    Args:
        field_name: Stored name of the list-valued annotation field.
        field_value: Value that the field must contain.

    Returns:
        An SQL condition on the annotation's stored list values.
    """
    return exists(
        select(1).where(
            AnnotationMultivaluedFieldValueRecord.annotation_id
            == AnnotationRecord.annotation_id,
            AnnotationMultivaluedFieldValueRecord.field_name == field_name,
            AnnotationMultivaluedFieldValueRecord.field_value == field_value,
        )
    )


def _criteria_conditions(criteria: AnnotationFilter) -> list[ColumnElement[bool]]:
    """Build the SQL conditions for every supplied search criterion.

    Args:
        criteria: Field values that matching annotations must have.

    Returns:
        SQL conditions that a matching annotation must satisfy together.
    """
    conditions: list[ColumnElement[bool]] = []
    if criteria.db_object_id is not None:
        conditions.append(AnnotationRecord.db_object_id == criteria.db_object_id)
    if criteria.negation is not None:
        conditions.append(AnnotationRecord.negation == criteria.negation)
    if criteria.relation is not None:
        conditions.append(AnnotationRecord.relation == criteria.relation)
    if criteria.ontology_class_id is not None:
        if criteria.ontology_class_id_closure is not None:
            conditions.append(
                _closure_condition(
                    criteria.ontology_class_id, criteria.ontology_class_id_closure
                )
            )
        else:
            conditions.append(
                AnnotationRecord.ontology_class_id == criteria.ontology_class_id
            )
    if criteria.evidence_type is not None:
        conditions.append(AnnotationRecord.evidence_type == criteria.evidence_type)
    if criteria.annotation_date is not None:
        conditions.append(AnnotationRecord.annotation_date == criteria.annotation_date)
    if criteria.assigned_by is not None:
        conditions.append(AnnotationRecord.assigned_by == criteria.assigned_by)
    conditions.extend(
        _multivalued_condition("references", value) for value in criteria.references
    )
    conditions.extend(
        _multivalued_condition("with_or_from", value) for value in criteria.with_or_from
    )
    conditions.extend(
        _multivalued_condition("interacting_taxon_id", value)
        for value in criteria.interacting_taxon_id
    )
    return conditions


def _ownership_conditions(scope: OwnershipScope) -> list[ColumnElement[bool]]:
    """Build ownership conditions to apply before pagination.

    Args:
        scope: Group and first-version creator that results must match.

    Returns:
        SQL conditions for the requested ownership restrictions.
    """
    conditions: list[ColumnElement[bool]] = []
    if scope.owning_group_id is not None:
        conditions.append(AnnotationRecord.owning_group_id == scope.owning_group_id)
    if scope.created_by is not None:
        conditions.append(
            exists(
                select(1).where(
                    AnnotationVersionRecord.annotation_id
                    == AnnotationRecord.annotation_id,
                    AnnotationVersionRecord.version == 1,
                    AnnotationVersionRecord.actor_id == scope.created_by,
                )
            )
        )
    return conditions


@dataclass(frozen=True, slots=True)
class PreparedCreate:
    """A new annotation whose write locks are held by the preparing transaction.

    Attributes:
        data: The annotation's stored form and duplicate lookup values.
        duplicate_peer_ids: Active annotations that duplicate it.
    """

    data: AnnotationPersistenceData
    duplicate_peer_ids: tuple[UUID, ...]
    session: Session = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """A locked annotation and its replacement, checked against the expected version.

    Attributes:
        record: The current annotation, locked for update.
        data: The replacement's stored form and duplicate lookup values.
        duplicate_peer_ids: Other active annotations that duplicate the
            replacement.
    """

    record: AnnotationRecord
    data: AnnotationPersistenceData
    duplicate_peer_ids: tuple[UUID, ...]
    session: Session = field(repr=False, compare=False)


class AnnotationRepository:
    """Read and write annotations in a caller-managed transaction.

    The repository flushes changes so database errors are raised promptly, but
    the caller remains responsible for committing or rolling back the session.

    Creates and updates happen in two steps. `prepare_create` and
    `prepare_update` take the locks a write needs and return the annotation's
    duplicate peers; the caller validates the annotation, and then `create` or
    `update` writes it. A write accepts only something prepared in the same
    session, so no write can skip the locks. `soft_delete` checks the expected
    version and takes its locks itself. All of them use shared private helpers for
    the underlying database writes so annotations and their version histories
    remain consistent.

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
                AnnotationRecord.status == AnnotationStatus.ACTIVE
            )
        return self.session.scalar(statement)

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
        return load_page(
            self.session,
            statement,
            record_type=AnnotationVersionRecord,
            order_by=(AnnotationVersionRecord.version,),
            limit=limit,
            offset=offset,
        )

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
        criteria: AnnotationFilter,
        scope: OwnershipScope,
        *,
        limit: int,
        offset: int,
    ) -> Page[AnnotationRecord]:
        """List active annotations that match every supplied criterion.

        Args:
            criteria: Field values that matching annotations must have.
            scope: Ownership restrictions applied before pagination.
            limit: Maximum number of annotations to return.
            offset: Number of matching annotations to skip.

        Returns:
            The selected annotation records and total number of matches.
        """
        statement = select(AnnotationRecord).where(
            AnnotationRecord.status == AnnotationStatus.ACTIVE,
            *_ownership_conditions(scope),
            *_criteria_conditions(criteria),
        )
        return load_page(
            self.session,
            statement,
            record_type=AnnotationRecord,
            order_by=(AnnotationRecord.created_at, AnnotationRecord.annotation_id),
            limit=limit,
            offset=offset,
        )

    def list_active_after(
        self,
        after: UUID | None,
        *,
        owning_group_id: str | None,
        limit: int,
    ) -> tuple[AnnotationRecord, ...]:
        """Return the next active annotations in `annotation_id` order, without locks.

        Args:
            after: Return only annotations whose ID is greater; `None` starts at
                the beginning.
            owning_group_id: Return only this group's annotations; `None` returns
                every group's.
            limit: The most annotations to return.
        """
        statement = select(AnnotationRecord).where(
            AnnotationRecord.status == AnnotationStatus.ACTIVE
        )
        if after is not None:
            statement = statement.where(AnnotationRecord.annotation_id > after)
        if owning_group_id is not None:
            statement = statement.where(
                AnnotationRecord.owning_group_id == owning_group_id
            )
        return tuple(
            self.session.scalars(
                statement.order_by(AnnotationRecord.annotation_id).limit(limit)
            )
        )

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
                AnnotationRecord.status == AnnotationStatus.ACTIVE,
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

    def prepare_create(self, annotation: Annotation) -> PreparedCreate:
        """Take the locks a create needs and find the annotation's duplicates.

        The shared global annotation-write lock and the candidate's signature lock
        are held until the transaction ends, so no concurrent write can create a
        duplicate of it before this transaction commits.

        Raises:
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        acquire_signature_locks(self.session, [data.duplicate_base_signature])
        return PreparedCreate(
            data=data,
            duplicate_peer_ids=self.find_duplicate_peer_ids(
                data.duplicate_base_signature, data.canonical_references
            ),
            session=self.session,
        )

    def create(
        self,
        prepared: PreparedCreate,
        *,
        actor_id: str,
        owning_group_id: str,
        change_source: ChangeSource = ChangeSource.API,
    ) -> AnnotationRecord:
        """Store a prepared annotation as version 1.

        Raises:
            ValueError: If `prepared` came from another session.
        """
        self._require_session(prepared.session)
        annotation_id = new_annotation_id()
        record = AnnotationRecord(
            annotation_id=annotation_id,
            current_version=1,
            status=AnnotationStatus.ACTIVE,
            deleted_at=None,
            owning_group_id=owning_group_id,
            record_origin=AnnotationOrigin.DIRECT,
            source_import_job_id=None,
            **prepared.data.column_values(),
        )
        self.session.add(record)
        self.session.flush([record])
        self.session.add(
            AnnotationVersionRecord(
                annotation_id=annotation_id,
                version=1,
                annotation_data=prepared.data.annotation_data,
                is_deleted=False,
                actor_id=actor_id,
                change_source=change_source,
            )
        )
        self._add_derived_values(annotation_id, prepared.data)
        self.session.flush()
        return record

    def prepare_update(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        expected_version: int,
    ) -> PreparedUpdate:
        """Lock an annotation for update and find its replacement's duplicates.

        Locks are taken in the order every annotation write uses: the shared
        global annotation-write lock, the annotation's row, then the signature
        locks of its current and replacement states. The expected version is
        checked after the row is locked, so it cannot change before the update.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation is deleted.
            StaleAnnotationVersionError: If `expected_version` is not current.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        current = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(current)
        # The row lock keeps `current` unchanged until this transaction ends, so
        # its stored signature is the one to lock.
        acquire_signature_locks(
            self.session,
            [current.duplicate_base_signature, data.duplicate_base_signature],
        )
        if current.current_version != expected_version:
            raise StaleAnnotationVersionError(
                annotation_id, expected_version, current.current_version
            )
        return PreparedUpdate(
            record=current,
            data=data,
            duplicate_peer_ids=self.find_duplicate_peer_ids(
                data.duplicate_base_signature,
                data.canonical_references,
                exclude_annotation_id=annotation_id,
            ),
            session=self.session,
        )

    def update(
        self,
        prepared: PreparedUpdate,
        *,
        actor_id: str,
        change_source: ChangeSource = ChangeSource.API,
    ) -> AnnotationRecord:
        """Save a prepared replacement as the annotation's next version.

        Raises:
            ValueError: If `prepared` came from another session.
        """
        self._require_session(prepared.session)
        return self._apply_update(
            prepared.record,
            prepared.data,
            actor_id=actor_id,
            change_source=change_source,
        )

    def _apply_update(
        self,
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
        *,
        actor_id: str,
        change_source: ChangeSource,
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
        expected_version: int,
        actor_id: str,
        change_source: ChangeSource = ChangeSource.API,
    ) -> AnnotationRecord:
        """Mark an annotation as deleted when its version matches.

        The annotation's database record and saved versions remain available for
        history queries, but current-annotation queries no longer return it.

        Args:
            annotation_id: Identifier of the annotation to delete.
            expected_version: Version supplied by the client.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow recorded in version history; defaults to API.

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

        if record.current_version != expected_version:
            raise StaleAnnotationVersionError(
                annotation_id,
                expected_version,
                record.current_version,
            )

        return self._apply_soft_delete(
            record,
            actor_id=actor_id,
            change_source=change_source,
        )

    def _apply_soft_delete(
        self,
        record: AnnotationRecord,
        *,
        actor_id: str,
        change_source: ChangeSource,
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
        record.status = AnnotationStatus.DELETED
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
            raise AnnotationNotFoundError(annotation_id)
        return record

    def _require_session(self, session: Session) -> None:
        """Reject a prepared write from another session, which holds no locks here."""
        if session is not self.session:
            raise ValueError("a prepared write must be used in its own session")

    @staticmethod
    def _raise_if_deleted(record: AnnotationRecord) -> None:
        if record.status == AnnotationStatus.DELETED:
            raise AnnotationDeletedError(record.annotation_id)

    @staticmethod
    def _update_current_record(
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
    ) -> None:
        for column, value in persistence_data.column_values().items():
            setattr(record, column, value)

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
            AnnotationMultivaluedFieldValueRecord(annotation_id=annotation_id, **row)
            for row in persistence_data.multivalued_rows()
        )
        self.session.add_all(
            AnnotationDuplicateReferenceRecord(annotation_id=annotation_id, **row)
            for row in persistence_data.reference_rows()
        )
