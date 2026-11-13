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
    ActiveSnapshots,
    AnnotationFacts,
    EntityFact,
    ReferenceDataUnavailableError,
    ReplacementFacts,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    split_identifier,
    term_identifiers,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
)
from standard_annotation_backend.domain.term_replacement import replacement_step
from standard_annotation_backend.persistence.models import (
    EntityCatalogSnapshotRecord,
    EntitySourceRecord,
    OntologyAltIdRecord,
    OntologyClosureRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
    RetractedPublicationRecord,
    RulesSnapshotRecord,
)

_QUERY_BATCH_SIZE = 5_000
RULES_REFERENCE_DATA = "rules"
"""The name `ReferenceDataUnavailableError` uses for the go-site rule data."""


class ValidationFactsRepository:
    """Read validation facts in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

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
        ontology or of the rule data, even if another one activates while it runs.
        Queries are batched, so one call can handle one annotation or thousands.

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
            snapshots: Snapshots the caller already looked up with
                `active_snapshots`, for example a QC report that records which
                ones it used. Looked up here when omitted.

        Returns:
            One `AnnotationFacts` per annotation.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot.
            StoredDataError: If the stored rule data cannot be read.
            ValueError: If `peers` and `annotations` differ in length.
        """
        if len(peers) != len(annotations):
            raise ValueError("every annotation needs its duplicate peers")
        if snapshots is None:
            snapshots = self.active_snapshots(ontologies)
        wanted: dict[UUID, set[str]] = {}
        for annotation in annotations:
            for occurrence in term_identifiers(annotation):
                version_id = _snapshot_for(occurrence.prefix, ontologies, snapshots)
                if version_id is not None:
                    wanted.setdefault(version_id, set()).add(occurrence.identifier)
        terms = self._term_facts(wanted)
        entities = self._entity_facts(
            {annotation.db_object_id for annotation in annotations}
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
                rule data has no active snapshot.
            StoredDataError: If the stored rule data cannot be read.
        """
        snapshots = self.active_snapshots(ontologies)
        terms: dict[str, TermFact] = {}
        pending = {
            occurrence.identifier
            for annotation in annotations
            for occurrence in term_identifiers(annotation)
        }
        while pending:
            wanted: dict[UUID, set[str]] = {}
            for identifier in pending:
                version_id = _snapshot_for(
                    split_identifier(identifier)[0], ontologies, snapshots
                )
                if version_id is not None:
                    wanted.setdefault(version_id, set()).add(identifier)
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

    def active_snapshots(self, ontologies: OntologyCatalog) -> ActiveSnapshots:
        """Return the active snapshot of each configured ontology and of the rule data.

        Args:
            ontologies: The configured ontologies. Each must have an active
                snapshot.

        Raises:
            ReferenceDataUnavailableError: If any of them has no active snapshot.
            StoredDataError: If the stored rule data cannot be read.
        """
        versions = {
            key: version_id
            for key, version_id in self.session.execute(
                select(
                    OntologyMetadataRecord.ontology_key,
                    OntologyMetadataRecord.version_id,
                ).where(
                    OntologyMetadataRecord.ontology_key.in_(ontologies.keys),
                    OntologyMetadataRecord.active.is_(True),
                )
            )
        }
        missing = set(ontologies.keys) - set(versions)
        rules = self.session.execute(
            select(
                RulesSnapshotRecord.snapshot_id, RulesSnapshotRecord.rule_data
            ).where(RulesSnapshotRecord.active.is_(True))
        ).one_or_none()
        if rules is None:
            missing.add(RULES_REFERENCE_DATA)
        if missing:
            raise ReferenceDataUnavailableError(missing)
        assert rules is not None
        return ActiveSnapshots(
            ontology_versions=versions,
            rules_snapshot_id=rules.snapshot_id,
            rule_data=STORED_RULE_DATA.load(rules.rule_data),
        )

    def reference_versions(self, snapshots: ActiveSnapshots) -> ReferenceDataVersions:
        """Return version details of `snapshots` and of the active entity catalogs.

        QC reports record these to show which data they checked against. Entity
        facts are not tied to a snapshot; they are always read from the active
        entity catalogs, so this lists the catalogs active when it runs.
        """
        ontology_rows = self.session.execute(
            select(
                OntologyMetadataRecord.ontology_key,
                OntologyMetadataRecord.version_id,
                OntologyMetadataRecord.document_version,
                OntologyMetadataRecord.source_revision,
            )
            .where(
                OntologyMetadataRecord.version_id.in_(
                    list(snapshots.ontology_versions.values())
                )
            )
            .order_by(OntologyMetadataRecord.ontology_key)
        )
        go_site_revision = self.session.scalar(
            select(RulesSnapshotRecord.source_revision).where(
                RulesSnapshotRecord.snapshot_id == snapshots.rules_snapshot_id
            )
        )
        catalogs = self.session.execute(
            select(
                EntityCatalogSnapshotRecord.source_key,
                EntityCatalogSnapshotRecord.snapshot_id,
            )
            .where(EntityCatalogSnapshotRecord.active.is_(True))
            .order_by(EntityCatalogSnapshotRecord.source_key)
        )
        return ReferenceDataVersions(
            ontologies=tuple(
                OntologySnapshotUse(
                    ontology_key=key,
                    version_id=version_id,
                    document_version=document_version,
                    source_revision=source_revision,
                )
                for key, version_id, document_version, source_revision in ontology_rows
            ),
            go_site_revision=go_site_revision,
            entity_catalogs=tuple(
                EntityCatalogUse(source_key=source_key, snapshot_id=snapshot_id)
                for source_key, snapshot_id in catalogs
            ),
        )

    def _term_facts(self, wanted: dict[UUID, set[str]]) -> dict[str, TermFact]:
        """Return a fact for each wanted identifier from its ontology's snapshot.

        An identifier that names a term gets the status `current` or `obsolete`.
        One that is an alternative identifier gets `alternate`, and any other gets
        `unknown`.

        Args:
            wanted: The identifiers to look up, keyed by the version ID of the
                snapshot to read them from.
        """
        facts: dict[str, TermFact] = {}
        for version_id, identifiers in wanted.items():
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
                        OntologyTermRecord.version_id == version_id,
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
                        OntologyAltIdRecord.version_id == version_id,
                        OntologyAltIdRecord.alt_id.in_(batch),
                    )
                )
                for alt_id, term_id in rows:
                    facts[alt_id] = TermFact(TermStatus.ALTERNATE, primary_id=term_id)
            for identifier in remaining:
                facts.setdefault(identifier, TermFact(TermStatus.UNKNOWN))
        return facts

    def _entity_facts(self, identifiers: set[str]) -> dict[str, EntityFact]:
        """Return the taxon and type of each identifier an entity catalog lists.

        A source record exists only while its identifier is active. A catalog
        published before repeated identifiers were rejected may list one
        identifier on several lines; its first line is used.
        """
        facts: dict[str, EntityFact] = {}
        for batch in batched(sorted(identifiers), _QUERY_BATCH_SIZE, strict=False):
            rows = self.session.execute(
                select(
                    EntitySourceRecord.db_object_id,
                    EntitySourceRecord.db_object_taxon_id,
                    EntitySourceRecord.db_object_type,
                )
                .where(EntitySourceRecord.db_object_id.in_(batch))
                .order_by(
                    EntitySourceRecord.db_object_id, EntitySourceRecord.line_number
                )
            )
            for db_object_id, taxon, entity_type in rows:
                facts.setdefault(db_object_id, EntityFact(taxon, entity_type))
        return facts

    def _checked_ancestors(
        self,
        version_id: UUID,
        term_ids: Collection[str],
        checked_ancestors: frozenset[str],
    ) -> dict[str, frozenset[str]]:
        """Return the checked ancestors that each term is or descends from in GO.

        Descent follows `rdfs:subClassOf` in one GO snapshot. The closure table,
        which stores each term's ancestors, also links every term to itself, so a
        term that is itself a checked ancestor lists itself.

        Args:
            version_id: The GO snapshot to read.
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
                    OntologyClosureRecord.version_id == version_id,
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
