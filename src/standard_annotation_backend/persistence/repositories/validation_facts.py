"""Gather the facts semantic validation needs from the active reference data."""

from collections.abc import Iterable, Mapping, Sequence
from itertools import batched
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    EntityFact,
    ReferenceDataUnavailableError,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    term_identifiers,
)
from standard_annotation_backend.persistence.models import (
    EntitySourceRecord,
    OntologyAltIdRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
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
        ontologies: Mapping[str, tuple[str, ...]],
    ) -> tuple[AnnotationFacts, ...]:
        """Return the facts about each annotation, in the same order.

        The active snapshots are resolved once, first, and every later query reads
        only those snapshots. One call therefore never mixes two snapshots of an
        ontology, even if another one activates while it runs. Queries are batched,
        so one call serves one annotation or thousands.

        Args:
            annotations: Structurally valid annotations to describe.
            peers: Each annotation's duplicate peer IDs, in the same order. A
                write's peers come from its prepare step, taken under locks.
            ontologies: The identifier prefixes of each configured ontology, keyed
                by ontology key.

        Returns:
            One `AnnotationFacts` per annotation.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot.
            ValueError: If `peers` and `annotations` differ in length.
        """
        if len(peers) != len(annotations):
            raise ValueError("every annotation needs its duplicate peers")
        versions = self._active_versions(ontologies)
        version_by_prefix = {
            prefix: versions[key]
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
        return tuple(
            AnnotationFacts(
                terms={
                    occurrence.identifier: terms[occurrence.identifier]
                    for occurrence in term_identifiers(annotation)
                    if occurrence.identifier in terms
                },
                entity=entities.get(annotation.db_object_id),
                duplicate_peer_ids=annotation_peers,
            )
            for annotation, annotation_peers in zip(annotations, peers, strict=True)
        )

    def _active_versions(
        self, ontologies: Mapping[str, Iterable[str]]
    ) -> dict[str, UUID]:
        """Return each configured ontology's active snapshot, and check the rule data.

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
        rules = self.session.scalar(
            select(RulesSnapshotRecord.snapshot_id).where(
                RulesSnapshotRecord.active.is_(True)
            )
        )
        if rules is None:
            missing.add(RULES_REFERENCE_DATA)
        if missing:
            raise ReferenceDataUnavailableError(missing)
        return versions

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
                    ).where(
                        OntologyTermRecord.version_id == version_id,
                        OntologyTermRecord.term_id.in_(batch),
                    )
                )
                for term_id, obsolete, replaced_by, consider in rows:
                    facts[term_id] = TermFact(
                        TermStatus.OBSOLETE if obsolete else TermStatus.CURRENT,
                        replaced_by=tuple(replaced_by),
                        consider=tuple(consider),
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
