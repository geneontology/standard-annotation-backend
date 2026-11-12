"""Verify that validation facts come from one set of active snapshots."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from seeding import SHIPPED_SOURCES, SeedTerm, _succeeded_job
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.domain.semantic_validation.facts import (
    EntityFact,
    ReferenceDataUnavailableError,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.persistence.models import (
    OntologyMetadataRecord,
    OntologyTermRecord,
    RulesSnapshotRecord,
)
from standard_annotation_backend.persistence.repositories import (
    ValidationFactsRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory

ONTOLOGIES = {
    key: SHIPPED_SOURCES.ontology_definition(key).identifier_prefixes
    for key in SHIPPED_SOURCES.keys(RefreshKindName.ONTOLOGY)
}
PEER = UUID("00000000-0000-0000-0000-000000000001")


def _annotation(**changes: object) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-11-11",
            "assigned_by": "GO_Central",
            **changes,
        }
    )


def test_facts_describe_terms_entities_and_peers_from_active_snapshots(
    unit_of_work_factory: UnitOfWorkFactory,
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
) -> None:
    """Each identifier with a loaded prefix gets its status; others get none.

    The subject's taxon and type come from its catalog, and peers pass through.
    """
    seed_active_subjects("UniProtKB:P12345")
    reference_data(
        SeedTerm("GO:0000003", obsolete=True, replaced_by=("GO:0008150",)),
        SeedTerm("CL:0000000", alt_ids=("CL:0000099",)),
    )
    annotations = [
        _annotation(
            annotation_extensions=[
                {"extension_relation": "BFO:0000066", "extension_term": "CL:0000099"},
                {"extension_relation": "BFO:0000066", "extension_term": "CL:9999999"},
            ],
            with_or_from=["UniProtKB:Q1", "GO:0000003"],
        ),
        _annotation(db_object_id="UniProtKB:UNKNOWN"),
    ]

    with unit_of_work_factory() as uow:
        first, second = uow.validation_facts.gather(
            annotations, [(PEER,), ()], ONTOLOGIES
        )

    assert first.terms == {
        "GO:0008150": TermFact(TermStatus.CURRENT),
        "CL:0000099": TermFact(TermStatus.ALTERNATE, primary_id="CL:0000000"),
        "CL:9999999": TermFact(TermStatus.UNKNOWN),
        "GO:0000003": TermFact(TermStatus.OBSOLETE, replaced_by=("GO:0008150",)),
    }
    assert first.entity == EntityFact("NCBITaxon:9606", "PR:000000001")
    assert first.duplicate_peer_ids == (PEER,)
    assert second.entity is None
    assert second.duplicate_peer_ids == ()


@pytest.mark.parametrize("missing", ["go", "rules"])
def test_missing_reference_data_is_reported_by_name(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    reference_data: Callable[..., None],
    missing: str,
) -> None:
    """A configured ontology or rule data with no active snapshot stops gathering."""
    with session_factory.begin() as session:
        if missing == "rules":
            session.execute(update(RulesSnapshotRecord).values(active=False))
        else:
            session.execute(
                update(OntologyMetadataRecord)
                .where(OntologyMetadataRecord.ontology_key == missing)
                .values(active=False)
            )

    with (
        unit_of_work_factory() as uow,
        pytest.raises(ReferenceDataUnavailableError) as raised,
    ):
        uow.validation_facts.gather([_annotation()], [()], ONTOLOGIES)

    assert raised.value.missing == (missing,)


def test_an_activation_during_gathering_does_not_mix_snapshots(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    reference_data: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facts come from the snapshots active when gathering started.

    Another connection activates a GO snapshot in which the term is obsolete,
    after the snapshots are resolved and before terms are read.
    """
    original = ValidationFactsRepository._term_facts

    def activate_then_read(
        self: ValidationFactsRepository, wanted: dict[UUID, set[str]]
    ) -> dict[str, TermFact]:
        with session_factory.begin() as other:
            other.execute(
                update(OntologyMetadataRecord)
                .where(
                    OntologyMetadataRecord.ontology_key == "go",
                    OntologyMetadataRecord.active.is_(True),
                )
                .values(active=False)
            )
            newer = OntologyMetadataRecord(
                version_id=uuid4(),
                ontology_key="go",
                source_type="https",
                source_locator="https://example.org/go-newer.obo",
                source_revision=None,
                source_checksum="d" * 64,
                document_version=None,
                loaded_predicates=["rdfs:subClassOf"],
                identifier_prefixes=["GO"],
                refresh_result=None,
                job_id=_succeeded_job(other, "ontology_refresh", "go"),
                fetched_at=datetime.now(UTC),
                active=True,
            )
            other.add(newer)
            other.flush([newer])
            other.add(
                OntologyTermRecord(
                    version_id=newer.version_id,
                    term_id="GO:0008150",
                    obsolete=True,
                    replaced_by=[],
                    consider=[],
                    subsets=[],
                )
            )
        return original(self, wanted)

    monkeypatch.setattr(ValidationFactsRepository, "_term_facts", activate_then_read)
    with unit_of_work_factory() as uow:
        (facts,) = uow.validation_facts.gather([_annotation()], [()], ONTOLOGIES)

    assert facts.terms["GO:0008150"] == TermFact(TermStatus.CURRENT)
