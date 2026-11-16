"""Read the facts that semantic validation needs from the active reference data.

A fact is a value a check reads about an annotation, such as whether a term is
obsolete, the entity's type, or whether a cited publication is retracted. This
module reads the facts from the database; the checks themselves run without one.
"""

from collections.abc import Collection, Sequence
from dataclasses import replace
from itertools import batched
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    SUBCLASS_PREDICATE,
    OntologyCatalog,
)
from standard_annotation_backend.domain.qc_reports import (
    EntityCatalogUse,
    OntologySnapshotUse,
    ReferenceDataVersions,
)
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    checked_ancestor_terms,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    RULES_REFERENCE_DATA,
    ActiveSnapshots,
    AnnotationFacts,
    EntityFact,
    ReplacementFacts,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    split_identifier,
    term_identifiers,
)
from standard_annotation_backend.domain.term_replacement import replacement_step
from standard_annotation_backend.persistence.models import (
    OntologyAltIdRecord,
    OntologyClosureRecord,
    OntologySnapshotDetailRecord,
    OntologyTermRecord,
    ReferenceSnapshotRecord,
    RetractedPublicationRecord,
    StoredEntityRecord,
)
from standard_annotation_backend.persistence.repositories.snapshots import (
    SnapshotRepository,
)

_QUERY_BATCH_SIZE = 5_000


class ValidationFactsRepository:
    """Read validation facts in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._snapshots = SnapshotRepository(session)

    def gather(
        self,
        annotations: Sequence[Annotation],
        peers: Sequence[tuple[UUID, ...]],
        ontologies: OntologyCatalog,
        *,
        snapshots: ActiveSnapshots | None = None,
    ) -> tuple[AnnotationFacts, ...]:
        """Return the facts about each annotation, in the same order.

        The active snapshots are looked up once, first, and every later query reads
        only those snapshots. One call therefore never mixes two snapshots of an
        ontology, the rule data, or an entity catalog, even if another one
        activates while it runs. Queries are batched, so one call can handle one
        annotation or thousands.

        Besides each identifier's status, the facts include the subsets of each
        term, the checked ancestors of each current GO term and of the entity's
        type, the rule data, and the references listed as retracted. Checked
        ancestors are the GO terms, such as the three GO roots, for which some rule
        asks whether a term is that term or one of its descendants;
        `checked_ancestor_terms` lists them.

        Args:
            annotations: Structurally valid annotations to describe.
            peers: Each annotation's duplicate peers, the IDs of other active
                annotations it duplicates, in the same order. For a write they
                come from `AnnotationRepository.lock_for_create` or
                `lock_for_update`, which find them while holding the write locks.
            ontologies: The configured ontologies and the identifier prefixes each
                owns.
            snapshots: Snapshots the caller already resolved with
                `SnapshotRepository.resolve`, for example a QC report that
                records which ones it used. They must still be readable;
                resolved here when omitted.

        Returns:
            One `AnnotationFacts` per annotation.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot, or the active rule data was
                stored by an earlier reader version and can no longer be decoded.
            SnapshotPrunedError: If `snapshots` were given and one of them was
                pruned since it was resolved.
            StoredDataError: If rule data stored by this release's reader
                version cannot be decoded.
            ValueError: If `peers` and `annotations` differ in length.
        """
        if len(peers) != len(annotations):
            raise ValueError("every annotation needs its duplicate peers")
        if snapshots is None:
            snapshots = self._snapshots.resolve(ontologies)
        else:
            self._snapshots.require_readable(snapshots.snapshot_ids)
        wanted: dict[UUID, set[str]] = {}
        for annotation in annotations:
            for occurrence in term_identifiers(annotation):
                snapshot_id = _snapshot_for(occurrence.prefix, ontologies, snapshots)
                if snapshot_id is not None:
                    wanted.setdefault(snapshot_id, set()).add(occurrence.identifier)
        terms = self._term_facts(wanted)
        entities = self._entity_facts(
            {annotation.db_object_id for annotation in annotations},
            snapshots.entity_catalogs.values(),
        )
        go_prefixes = ontologies.go_prefixes
        current_go_terms = {
            identifier
            for identifier, fact in terms.items()
            if fact.status is TermStatus.CURRENT
            and split_identifier(identifier)[0] in go_prefixes
        }
        go_types = {
            entity.db_object_type
            for entity in entities.values()
            if split_identifier(entity.db_object_type)[0] in go_prefixes
        }
        checked_ancestors = self._checked_ancestors(
            snapshots.ontology_versions[ontologies.go_key],
            current_go_terms | go_types,
            checked_ancestor_terms(snapshots.rule_data),
        )
        terms = {
            identifier: (
                replace(
                    fact,
                    checked_ancestors=checked_ancestors.get(identifier, frozenset()),
                )
                if identifier in current_go_terms
                else fact
            )
            for identifier, fact in terms.items()
        }
        entities = {
            identifier: replace(
                entity,
                type_checked_ancestors=checked_ancestors.get(
                    entity.db_object_type, frozenset()
                ),
            )
            for identifier, entity in entities.items()
        }
        retracted = self._retracted(
            snapshots.rules_snapshot_id,
            {
                reference
                for annotation in annotations
                for reference in annotation.references
            },
        )
        return tuple(
            AnnotationFacts(
                terms={
                    occurrence.identifier: terms[occurrence.identifier]
                    for occurrence in term_identifiers(annotation)
                    if occurrence.identifier in terms
                },
                entity=entities.get(annotation.db_object_id),
                duplicate_peer_ids=annotation_peers,
                rule_data=snapshots.rule_data,
                retracted_pmids=frozenset(
                    reference
                    for reference in annotation.references
                    if reference in retracted
                ),
            )
            for annotation, annotation_peers in zip(annotations, peers, strict=True)
        )

    def replacement_facts(
        self,
        annotations: Sequence[Annotation],
        ontologies: OntologyCatalog,
    ) -> ReplacementFacts:
        """Return facts about the annotations' terms and the terms that replace them.

        A replacement chain starts at an identifier an annotation uses and steps
        from an alternative ID to its primary term, or from an obsolete term to its
        only `replaced_by` term. The active snapshots are looked up once, and each
        identifier is read from the snapshot of the ontology that owns its prefix.
        Reading continues along every chain until it reaches a term already read,
        a term with no further step, or an identifier no configured ontology owns.

        Args:
            annotations: Structurally valid annotations to describe.
            ontologies: The configured ontologies and the identifier prefixes each
                owns.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot, or the active rule data was
                stored by an earlier reader version and can no longer be decoded.
            StoredDataError: If rule data stored by this release's reader
                version cannot be decoded.
        """
        snapshots = self._snapshots.resolve(ontologies)
        terms: dict[str, TermFact] = {}
        pending = {
            occurrence.identifier
            for annotation in annotations
            for occurrence in term_identifiers(annotation)
        }
        while pending:
            wanted: dict[UUID, set[str]] = {}
            for identifier in pending:
                snapshot_id = _snapshot_for(
                    split_identifier(identifier)[0], ontologies, snapshots
                )
                if snapshot_id is not None:
                    wanted.setdefault(snapshot_id, set()).add(identifier)
            found = self._term_facts(wanted)
            terms.update(found)
            pending = {
                following
                for fact in found.values()
                if (following := replacement_step(fact)) is not None
                and following not in terms
            }
        return ReplacementFacts(
            terms=terms,
            snapshot_ids={
                **snapshots.ontology_versions,
                RULES_REFERENCE_DATA: snapshots.rules_snapshot_id,
            },
        )

    def reference_versions(self, snapshots: ActiveSnapshots) -> ReferenceDataVersions:
        """Return version details of `snapshots`, as QC reports record them."""
        ontology_rows = self.session.execute(
            select(
                ReferenceSnapshotRecord.source_key,
                ReferenceSnapshotRecord.snapshot_id,
                OntologySnapshotDetailRecord.document_version,
                ReferenceSnapshotRecord.source_revision,
            )
            .join(
                OntologySnapshotDetailRecord,
                OntologySnapshotDetailRecord.snapshot_id
                == ReferenceSnapshotRecord.snapshot_id,
            )
            .where(
                ReferenceSnapshotRecord.snapshot_id.in_(
                    sorted(snapshots.ontology_versions.values())
                )
            )
            .order_by(ReferenceSnapshotRecord.source_key)
        )
        go_site_revision = self.session.scalar(
            select(ReferenceSnapshotRecord.source_revision).where(
                ReferenceSnapshotRecord.snapshot_id == snapshots.rules_snapshot_id
            )
        )
        return ReferenceDataVersions(
            ontologies=tuple(
                OntologySnapshotUse(
                    ontology_key=key,
                    version_id=snapshot_id,
                    document_version=document_version,
                    source_revision=source_revision,
                )
                for key, snapshot_id, document_version, source_revision in (
                    ontology_rows
                )
            ),
            go_site_revision=go_site_revision,
            entity_catalogs=tuple(
                EntityCatalogUse(source_key=source_key, snapshot_id=snapshot_id)
                for source_key, snapshot_id in sorted(snapshots.entity_catalogs.items())
            ),
        )

    def _term_facts(self, wanted: dict[UUID, set[str]]) -> dict[str, TermFact]:
        """Return a fact for each wanted identifier from its ontology's snapshot.

        An identifier that names a term gets the status `current` or `obsolete`.
        One that is an alternative identifier gets `alternate`, and any other gets
        `unknown`.

        Args:
            wanted: The identifiers to look up, keyed by the ID of the snapshot
                to read them from.
        """
        facts: dict[str, TermFact] = {}
        for snapshot_id, identifiers in wanted.items():
            ordered = sorted(identifiers)
            for batch in batched(ordered, _QUERY_BATCH_SIZE, strict=False):
                rows = self.session.execute(
                    select(
                        OntologyTermRecord.term_id,
                        OntologyTermRecord.obsolete,
                        OntologyTermRecord.replaced_by,
                        OntologyTermRecord.consider,
                        OntologyTermRecord.subsets,
                    ).where(
                        OntologyTermRecord.snapshot_id == snapshot_id,
                        OntologyTermRecord.term_id.in_(batch),
                    )
                )
                for term_id, obsolete, replaced_by, consider, subsets in rows:
                    facts[term_id] = TermFact(
                        TermStatus.OBSOLETE if obsolete else TermStatus.CURRENT,
                        replaced_by=tuple(replaced_by),
                        consider=tuple(consider),
                        subsets=frozenset(subsets),
                    )
            remaining = [
                identifier for identifier in ordered if identifier not in facts
            ]
            for batch in batched(remaining, _QUERY_BATCH_SIZE, strict=False):
                rows = self.session.execute(
                    select(
                        OntologyAltIdRecord.alt_id, OntologyAltIdRecord.term_id
                    ).where(
                        OntologyAltIdRecord.snapshot_id == snapshot_id,
                        OntologyAltIdRecord.alt_id.in_(batch),
                    )
                )
                for alt_id, term_id in rows:
                    facts[alt_id] = TermFact(TermStatus.ALTERNATE, primary_id=term_id)
            for identifier in remaining:
                facts.setdefault(identifier, TermFact(TermStatus.UNKNOWN))
        return facts

    def _entity_facts(
        self, identifiers: set[str], snapshot_ids: Collection[UUID]
    ) -> dict[str, EntityFact]:
        """Return the taxon and type of each identifier the given catalogs list.

        An identifier is active in at most one catalog, so each has at most one
        fact.
        """
        catalogs = sorted(snapshot_ids)
        facts: dict[str, EntityFact] = {}
        if not catalogs:
            return facts
        for batch in batched(sorted(identifiers), _QUERY_BATCH_SIZE, strict=False):
            rows = self.session.execute(
                select(
                    StoredEntityRecord.db_object_id,
                    StoredEntityRecord.db_object_taxon_id,
                    StoredEntityRecord.db_object_type,
                ).where(
                    StoredEntityRecord.snapshot_id.in_(catalogs),
                    StoredEntityRecord.db_object_id.in_(batch),
                )
            )
            for db_object_id, taxon, entity_type in rows:
                facts[db_object_id] = EntityFact(taxon, entity_type)
        return facts

    def _checked_ancestors(
        self,
        snapshot_id: UUID,
        term_ids: Collection[str],
        checked_ancestors: frozenset[str],
    ) -> dict[str, frozenset[str]]:
        """Return the checked ancestors that each term is or descends from in GO.

        Descent follows `rdfs:subClassOf` in one GO snapshot. The closure table,
        which stores each term's ancestors, also links every term to itself, so a
        term that is itself a checked ancestor lists itself.

        Args:
            snapshot_id: The GO snapshot to read.
            term_ids: The terms to look up.
            checked_ancestors: The GO terms to test each term against.
        """
        found: dict[str, set[str]] = {}
        for batch in batched(sorted(term_ids), _QUERY_BATCH_SIZE, strict=False):
            rows = self.session.execute(
                select(
                    OntologyClosureRecord.subject_term_id,
                    OntologyClosureRecord.object_term_id,
                ).where(
                    OntologyClosureRecord.snapshot_id == snapshot_id,
                    OntologyClosureRecord.predicate_id == SUBCLASS_PREDICATE,
                    OntologyClosureRecord.subject_term_id.in_(batch),
                    OntologyClosureRecord.object_term_id.in_(sorted(checked_ancestors)),
                )
            )
            for subject, ancestor in rows:
                found.setdefault(subject, set()).add(ancestor)
        return {
            term_id: frozenset(found_ancestors)
            for term_id, found_ancestors in found.items()
        }

    def _retracted(self, snapshot_id: UUID, references: Collection[str]) -> set[str]:
        """Return which of `references` are PMIDs listed as retracted."""
        pmids = sorted(
            reference for reference in references if reference.startswith("PMID:")
        )
        retracted: set[str] = set()
        for batch in batched(pmids, _QUERY_BATCH_SIZE, strict=False):
            retracted.update(
                self.session.scalars(
                    select(RetractedPublicationRecord.pmid).where(
                        RetractedPublicationRecord.snapshot_id == snapshot_id,
                        RetractedPublicationRecord.pmid.in_(batch),
                    )
                )
            )
        return retracted


def _snapshot_for(
    prefix: str, ontologies: OntologyCatalog, snapshots: ActiveSnapshots
) -> UUID | None:
    """Return the snapshot of the ontology that owns `prefix`, or `None`."""
    key = ontologies.key_of(prefix)
    return None if key is None else snapshots.ontology_versions[key]
