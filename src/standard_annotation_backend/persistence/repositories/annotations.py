"""Read and write annotations in caller-managed transactions."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    AnnotationStatus,
    new_annotation_id,
)
from standard_annotation_backend.domain.ontology import OntologyKey
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
from standard_annotation_backend.persistence.repositories.entities import (
    EntityRepository,
)
from standard_annotation_backend.persistence.repositories.pagination import (
    Page,
    load_page,
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
        owning_group_id: Group whose annotations may be returned.
        created_by: Actor recorded in the immutable first annotation version.
    """

    db_object_id: str | None = None
    negation: bool | None = None
    relation: str | None = None
    ontology_class_id: str | None = None
    ontology_class_id_closure: str | None = None
    evidence_type: str | None = None
    annotation_date: date | None = None
    assigned_by: str | None = None
    references: tuple[str, ...] = ()
    with_or_from: tuple[str, ...] = ()
    interacting_taxon_id: tuple[str, ...] = ()
    owning_group_id: str | None = None
    created_by: str | None = None


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


def _ownership_conditions(
    owning_group_id: str | None, created_by: str | None
) -> tuple[ColumnElement[bool], ...]:
    """Build ownership conditions to apply before pagination.

    Args:
        owning_group_id: Group key that returned annotations must match.
        created_by: Actor on the first annotation version that results must match.

    Returns:
        SQL conditions for the requested ownership restrictions.
    """
    conditions: list[ColumnElement[bool]] = []
    if owning_group_id is not None:
        conditions.append(AnnotationRecord.owning_group_id == owning_group_id)
    if created_by is not None:
        conditions.append(
            exists(
                select(1).where(
                    AnnotationVersionRecord.annotation_id
                    == AnnotationRecord.annotation_id,
                    AnnotationVersionRecord.version == 1,
                    AnnotationVersionRecord.actor_id == created_by,
                )
            )
        )
    return tuple(conditions)


class AnnotationRepository:
    """Read and write annotations in a caller-managed transaction.

    The repository flushes changes so database errors are raised promptly, but
    the caller remains responsible for committing or rolling back the session.

    `create`, `update`, and `soft_delete` serve both direct API writes and
    change-set acceptance. They enforce an active entity catalog subject,
    duplicate prevention, and expected-version checks. `apply_system_update`
    serves system-wide updates such as ontology refresh, whose caller holds the
    exclusive annotation lock and checks duplicates for the whole batch. All of
    them use shared private helpers for the underlying database writes so
    annotations and their version histories remain consistent.

    Args:
        session: Session used for every query and write.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._entities = EntityRepository(session)

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
            AnnotationRecord.status == AnnotationStatus.ACTIVE,
            *_ownership_conditions(filters.owning_group_id, filters.created_by),
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
                if (
                    field_name == "ontology_class_id"
                    and filters.ontology_class_id_closure is not None
                ):
                    statement = statement.where(
                        exists(
                            select(1)
                            .select_from(OntologyClosureRecord)
                            .join(
                                OntologyMetadataRecord,
                                OntologyMetadataRecord.version_id
                                == OntologyClosureRecord.version_id,
                            )
                            .where(
                                OntologyMetadataRecord.ontology_key
                                == OntologyKey.GO.value,
                                OntologyMetadataRecord.active.is_(True),
                                OntologyClosureRecord.subject_term_id
                                == AnnotationRecord.ontology_class_id,
                                OntologyClosureRecord.object_term_id == value,
                                OntologyClosureRecord.predicate_id
                                == filters.ontology_class_id_closure,
                            )
                        )
                    )
                    continue
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

        return load_page(
            self.session,
            statement,
            record_type=AnnotationRecord,
            order_by=(AnnotationRecord.created_at, AnnotationRecord.annotation_id),
            limit=limit,
            offset=offset,
        )

    def lock_all_active_for_system_update(self) -> tuple[AnnotationRecord, ...]:
        """Lock and return all active annotations for a system-wide update."""
        return tuple(
            self.session.scalars(
                select(AnnotationRecord)
                .where(AnnotationRecord.status == AnnotationStatus.ACTIVE)
                .order_by(AnnotationRecord.annotation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )

    def acquire_exclusive_system_update_lock(self) -> None:
        """Block annotation writes during a system-wide annotation update."""
        acquire_global_annotation_write_lock(self.session, exclusive=True)

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

    def create(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        owning_group_id: str,
        change_source: str = "api",
    ) -> AnnotationRecord:
        """Store an annotation unless an equivalent one is active.

        Args:
            annotation: Validated annotation to store.
            actor_id: Identifier for the person or process creating the annotation.
            owning_group_id: Identifier of the responsible group.
            change_source: Workflow recorded in version history; defaults to API.

        Returns:
            The newly stored annotation at version 1.

        Raises:
            UnknownDbObjectIdError: If `db_object_id` is not in the active entity
                catalog.
            DuplicateAnnotationError: If an equivalent active annotation exists.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        # Lock the subject's catalog membership before the annotation locks, the
        # order every annotation write uses, so catalog removal waits for us.
        self._entities.require_active(annotation.db_object_id)
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
        annotation_id = new_annotation_id()
        record = AnnotationRecord(
            annotation_id=annotation_id,
            current_version=1,
            status=AnnotationStatus.ACTIVE,
            deleted_at=None,
            owning_group_id=owning_group_id,
            record_origin=AnnotationOrigin.DIRECT,
            source_import_job_id=None,
            **persistence_data.column_values(),
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
        expected_version: int,
        actor_id: str,
        change_source: str = "api",
    ) -> AnnotationRecord:
        """Update an annotation when the client version is current.

        Args:
            annotation_id: Identifier of the annotation to update.
            annotation: Validated replacement annotation.
            expected_version: Version supplied by the client.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow recorded in version history; defaults to API.

        Returns:
            The updated annotation with a newly saved version.

        Raises:
            UnknownDbObjectIdError: If the replacement `db_object_id` is not in the
                active entity catalog.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            DuplicateAnnotationError: If the change creates a new duplicate.
            StaleAnnotationVersionError: If `expected_version` is not current.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        # Same lock order as `create`: catalog membership first.
        self._entities.require_active(annotation.db_object_id)
        candidate = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        current = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(current)
        before = prepare_annotation_for_persistence(
            Annotation.model_validate(current.annotation_data)
        )

        # The row lock taken above keeps `current` unchanged until this
        # transaction ends, so its signature is the one to lock and compare.
        acquire_signature_locks(
            self.session,
            [before.duplicate_base_signature, candidate.duplicate_base_signature],
        )

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
            change_source=change_source,
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

    def apply_system_update(
        self,
        record: AnnotationRecord,
        annotation: Annotation,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Update one annotation after system-wide validation is complete.

        The caller must hold the exclusive global annotation lock and evaluate
        duplicate safety for the complete batch before calling this method.

        Args:
            record: Current record of the annotation to update.
            annotation: Validated replacement annotation.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow recorded in version history.

        Returns:
            The updated current annotation record.
        """
        return self._apply_update(
            record,
            prepare_annotation_for_persistence(annotation),
            actor_id=actor_id,
            change_source=change_source,
        )

    def soft_delete(
        self,
        annotation_id: UUID,
        *,
        expected_version: int,
        actor_id: str,
        change_source: str = "api",
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
            raise AnnotationNotFoundError(f"annotation {annotation_id} was not found")
        return record

    @staticmethod
    def _raise_if_deleted(record: AnnotationRecord) -> None:
        if record.status == AnnotationStatus.DELETED:
            raise AnnotationDeletedError(
                f"annotation {record.annotation_id} is already deleted"
            )

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
