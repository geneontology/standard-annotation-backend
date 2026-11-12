"""Gather the facts semantic validation needs from the active reference data."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import batched
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    SUBCLASS_PREDICATE,
)
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    checked_ancestor_terms,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
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
    RuleData,
)
from standard_annotation_backend.domain.term_replacement import replacement_step
from standard_annotation_backend.persistence.models import (
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


@dataclass(frozen=True, slots=True)
class _ActiveSnapshots:
    """The snapshots one call reads, resolved once when it starts."""

    ontology_versions: Mapping[str, UUID]
    rules_snapshot_id: UUID
    rule_data: RuleData


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
        ontologies: Mapping[str, tuple[str, ...]],
    ) -> tuple[AnnotationFacts, ...]:
        """Return the facts about each annotation, in the same order.

        The active snapshots are resolved once, first, and every later query reads
        only those snapshots. One call therefore never mixes two snapshots of an
        ontology or of the rule data, even if another one activates while it runs.
        Queries are batched, so one call serves one annotation or thousands.

        Besides each identifier's status, the facts include the subsets of each
        term, the checked ancestors each current GO term and the entity's type fall under,
        the rule data, and the references listed as retracted.

        Args:
            annotations: Structurally valid annotations to describe.
            peers: Each annotation's duplicate peer IDs, in the same order. A
                write's peers come from its prepare step, taken under locks.
            ontologies: The identifier prefixes of each configured ontology, keyed
                by ontology key. GO must be among them.

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
        snapshots = self._active_snapshots(ontologies)
        version_by_prefix = {
            prefix: snapshots.ontology_versions[key]
            for key, prefixes in ontologies.items()
            for prefix in prefixes
        }
        wanted: dict[UUID, set[str]] = {}
        for annotation in annotations:
            for occurrence in term_identifiers(annotation):
                version_id = version_by_prefix.get(occurrence.prefix)
                if version_id is not None:
                    wanted.setdefault(version_id, set()).add(occurrence.identifier)
        terms = self._term_facts(wanted)
        entities = self._entity_facts(
            {annotation.db_object_id for annotation in annotations}
        )
        go_prefixes = set(ontologies[GO_ONTOLOGY_KEY])
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
            snapshots.ontology_versions[GO_ONTOLOGY_KEY],
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
        ontologies: Mapping[str, tuple[str, ...]],
    ) -> ReplacementFacts:
        """Return facts about the annotations' terms and their replacement chains.

        The active snapshots are resolved once. Each identifier is read from the
        snapshot of the ontology owning its prefix; then each obsolete term's only
        replacement and each alternative ID's primary term is read too, until no
        chain reaches a term not yet read.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot.
        """
        snapshots = self._active_snapshots(ontologies)
        version_by_prefix = {
            prefix: snapshots.ontology_versions[key]
            for key, prefixes in ontologies.items()
            for prefix in prefixes
        }
        terms: dict[str, TermFact] = {}
        pending = {
            occurrence.identifier
            for annotation in annotations
            for occurrence in term_identifiers(annotation)
            if occurrence.prefix in version_by_prefix
        }
        while pending:
            wanted: dict[UUID, set[str]] = {}
            for identifier in pending:
                version_id = version_by_prefix[split_identifier(identifier)[0]]
                wanted.setdefault(version_id, set()).add(identifier)
            found = self._term_facts(wanted)
            terms.update(found)
            pending = {
                following
                for fact in found.values()
                if (following := replacement_step(fact)) is not None
                and split_identifier(following)[0] in version_by_prefix
                and following not in terms
            }
        return ReplacementFacts(
            terms=terms,
            snapshot_ids={
                **snapshots.ontology_versions,
                RULES_REFERENCE_DATA: snapshots.rules_snapshot_id,
            },
        )

    def _active_snapshots(
        self, ontologies: Mapping[str, Iterable[str]]
    ) -> _ActiveSnapshots:
        """Return the active snapshots of every configured ontology and the rule data.

        Raises:
            ReferenceDataUnavailableError: If any of them has no active snapshot.
        """
        versions = {
            key: version_id
            for key, version_id in self.session.execute(
                select(
                    OntologyMetadataRecord.ontology_key,
                    OntologyMetadataRecord.version_id,
                ).where(
                    OntologyMetadataRecord.ontology_key.in_(list(ontologies)),
                    OntologyMetadataRecord.active.is_(True),
                )
            )
        }
        missing = set(ontologies) - set(versions)
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
        return _ActiveSnapshots(
            ontology_versions=versions,
            rules_snapshot_id=rules.snapshot_id,
            rule_data=STORED_RULE_DATA.load(rules.rule_data),
        )

    def _term_facts(self, wanted: dict[UUID, set[str]]) -> dict[str, TermFact]:
        """Return a fact for each wanted identifier in its ontology's snapshot.

        A term gives `current` or `obsolete`; otherwise an alternative identifier
        gives `alternate`; anything else is `unknown`.
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
        """Return the checked ancestors each term is, or descends from, in one GO snapshot.

        Every term has a reflexive closure row, so a term that is itself a checked ancestor
        lists it.
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
