"""Read and write annotations in caller-managed transactions."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import batched
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, select, tuple_
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
from standard_annotation_backend.domain.duplicate_policy import DuplicateKey
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

_PEER_BATCH_SIZE = 2_000
"""The most lookup values one query in a batched read sends to the database."""


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
class PeerQuery:
    """Ask which active annotations duplicate one annotation.

    Attributes:
        key: The annotation's duplicate key.
        exclude_id: A stored annotation to leave out of the answer: the
            annotation itself, or the annotation an update replaces. `None` for
            a new annotation.
    """

    key: DuplicateKey
    exclude_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LockedCreate:
    """Hold a new annotation after `AnnotationRepository.lock_for_create` locked it.

    `create` does not accept this value; it needs a `ValidatedCreate`, which
    shows that the annotation also passed validation while the locks were held.

    Attributes:
        data: The annotation's stored form and duplicate lookup values.
        duplicate_peer_ids: Active annotations that duplicate it.
        session: The session whose transaction holds the locks.
    """

    data: AnnotationPersistenceData
    duplicate_peer_ids: tuple[UUID, ...]
    session: Session = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class LockedUpdate:
    """Hold a locked annotation and its replacement after `lock_for_update`.

    `update` does not accept this value; it needs a `ValidatedUpdate`, which
    shows that the replacement also passed validation while the locks were held.

    Attributes:
        record: The current annotation, locked for update.
        data: The replacement's stored form and duplicate lookup values.
        duplicate_peer_ids: Other active annotations that duplicate the
            replacement.
        session: The session whose transaction holds the locks.
    """

    record: AnnotationRecord
    data: AnnotationPersistenceData
    duplicate_peer_ids: tuple[UUID, ...]
    session: Session = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ValidatedCreate:
    """A locked new annotation that passed every validation check.

    `create` accepts only this type, so no create can skip validation. In
    application code only `AnnotationValidator.prepare_create` builds one, after
    the annotation passed every check while the locks in `locked` were held.

    Attributes:
        locked: The annotation's stored form, duplicate peers, and the session
            whose transaction holds its locks.
    """

    locked: LockedCreate


@dataclass(frozen=True, slots=True)
class ValidatedUpdate:
    """A locked replacement annotation that passed every validation check.

    `update` accepts only this type, so no update can skip validation. In
    application code only `AnnotationValidator.prepare_update` builds one, after
    the replacement passed every check while the locks in `locked` were held.

    Attributes:
        locked: The current record, the replacement's stored form, its
            duplicate peers, and the session whose transaction holds the locks.
    """

    locked: LockedUpdate


class AnnotationRepository:
    """Read and write annotations in a caller-managed transaction.

    The repository flushes changes so database errors are raised promptly, but
    the caller remains responsible for committing or rolling back the session.

    Creates and updates happen in three steps. `lock_for_create` and
    `lock_for_update` take the locks a write needs and find the annotation's
    duplicate peers, the other active annotations it would duplicate.
    `AnnotationValidator` then checks the annotation and wraps the locked value
    in a `ValidatedCreate` or `ValidatedUpdate`. Only those are accepted by
    `create` and `update`, and only in the session that took the locks, so no
    write can skip the locks or the checks. `soft_delete` checks the expected
    version and takes its locks itself. All of them use shared private helpers
    for the underlying database writes so annotations and their version
    histories remain consistent.

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

        QC reports and term replacement jobs read annotations in batches by calling
        this repeatedly, passing the last ID of the previous batch as `after`.

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

    def duplicate_peers(
        self, queries: Sequence[PeerQuery]
    ) -> tuple[tuple[UUID, ...], ...]:
        """Return the active duplicate peers of each queried annotation.

        A peer shares the annotation's signature and at least one of its
        references. Lookups are batched, so one call can answer one query or
        thousands. Nothing is locked here: a write takes its signature locks
        first, so the answer cannot change before it commits; any other caller
        gets an answer that may already be out of date.

        Args:
            queries: The annotations to look up.

        Returns:
            For each query, in the same order, its peers' IDs in ascending order,
            without the query's `exclude_id`.
        """
        pairs = sorted(
            {
                (query.key.signature, reference)
                for query in queries
                for reference in query.key.references
            }
        )
        holders: dict[tuple[str, str], set[UUID]] = {}
        for batch in batched(pairs, _PEER_BATCH_SIZE, strict=False):
            rows = self.session.execute(
                select(
                    AnnotationDuplicateReferenceRecord.duplicate_base_signature,
                    AnnotationDuplicateReferenceRecord.canonical_reference,
                    AnnotationDuplicateReferenceRecord.annotation_id,
                )
                .join(
                    AnnotationRecord,
                    AnnotationRecord.annotation_id
                    == AnnotationDuplicateReferenceRecord.annotation_id,
                )
                .where(
                    AnnotationRecord.status == AnnotationStatus.ACTIVE,
                    tuple_(
                        AnnotationDuplicateReferenceRecord.duplicate_base_signature,
                        AnnotationDuplicateReferenceRecord.canonical_reference,
                    ).in_(batch),
                )
            )
            for signature, reference, annotation_id in rows:
                holders.setdefault((signature, reference), set()).add(annotation_id)
        return tuple(
            tuple(
                sorted(
                    {
                        peer
                        for reference in query.key.references
                        for peer in holders.get((query.key.signature, reference), ())
                    }
                    - {query.exclude_id}
                )
            )
            for query in queries
        )

    def creators(self, annotation_ids: Collection[UUID]) -> dict[UUID, str]:
        """Return the actor who created each annotation, from its first version.

        A caller whose authorization has `self` scope may read only the
        annotations it created.
        """
        found: dict[UUID, str] = {}
        for batch in batched(sorted(annotation_ids), _PEER_BATCH_SIZE, strict=False):
            rows = self.session.execute(
                select(
                    AnnotationVersionRecord.annotation_id,
                    AnnotationVersionRecord.actor_id,
                ).where(
                    AnnotationVersionRecord.annotation_id.in_(batch),
                    AnnotationVersionRecord.version == 1,
                )
            )
            found.update({annotation_id: actor for annotation_id, actor in rows})
        return found

    def lock_for_create(self, annotation: Annotation) -> LockedCreate:
        """Take the locks a create needs and find the annotation's duplicates.

        The global annotation-write lock is taken in shared mode, which lets other
        ordinary writes run but waits for exclusive operations such as a GPAD
        publication. The annotation's signature lock is also taken. Both are held
        until the transaction ends, so no concurrent write can create a duplicate
        of the annotation before this transaction commits. `create` accepts the
        result only after `AnnotationValidator` has checked the annotation.

        Raises:
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        acquire_signature_locks(self.session, [data.duplicate_base_signature])
        (peers,) = self.duplicate_peers(
            [
                PeerQuery(
                    DuplicateKey(
                        data.duplicate_base_signature, data.canonical_references
                    )
                )
            ]
        )
        return LockedCreate(data=data, duplicate_peer_ids=peers, session=self.session)

    def create(
        self,
        validated: ValidatedCreate,
        *,
        actor_id: str,
        owning_group_id: str,
        change_source: ChangeSource = ChangeSource.API,
    ) -> AnnotationRecord:
        """Store a validated annotation as version 1.

        Args:
            validated: The result of `AnnotationValidator.prepare_create` in this
                session.
            actor_id: Identifier for the person or process creating the annotation.
            owning_group_id: Identifier of the responsible group.
            change_source: Workflow recorded in version history; defaults to API.

        Raises:
            TypeError: If `validated` is not a `ValidatedCreate`.
            ValueError: If `validated` came from another session.
        """
        if not isinstance(validated, ValidatedCreate):
            raise TypeError("create needs an annotation that passed validation")
        prepared = validated.locked
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

    def lock_for_update(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        expected_version: int,
    ) -> LockedUpdate:
        """Lock an annotation for update and find its replacement's duplicates.

        Locks are taken in the order every annotation write uses: the shared
        global annotation-write lock, the annotation's row, then the signature
        locks of its current and replacement states. The expected version is
        checked after the row is locked, so it cannot change before the update.
        `update` accepts the result only after `AnnotationValidator` has checked
        the replacement.

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
        (peers,) = self.duplicate_peers(
            [
                PeerQuery(
                    DuplicateKey(
                        data.duplicate_base_signature, data.canonical_references
                    ),
                    exclude_id=annotation_id,
                )
            ]
        )
        return LockedUpdate(
            record=current, data=data, duplicate_peer_ids=peers, session=self.session
        )

    def update(
        self,
        validated: ValidatedUpdate,
        *,
        actor_id: str,
        change_source: ChangeSource = ChangeSource.API,
    ) -> AnnotationRecord:
        """Save a validated replacement as the annotation's next version.

        Args:
            validated: The result of `AnnotationValidator.prepare_update` in this
                session.
            actor_id: Identifier for the person or process making the change.
            change_source: Workflow recorded in version history; defaults to API.

        Raises:
            TypeError: If `validated` is not a `ValidatedUpdate`.
            ValueError: If `validated` came from another session.
        """
        if not isinstance(validated, ValidatedUpdate):
            raise TypeError("update needs an annotation that passed validation")
        prepared = validated.locked
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
        """Reject a locked write from another session, which holds no locks here."""
        if session is not self.session:
            raise ValueError("a locked write must be used in its own session")

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
