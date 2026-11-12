"""Verify that validation facts come from one set of active snapshots."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from seeding import (
    DEFAULT_RULE_DATA,
    SHIPPED_SOURCES,
    SeedTerm,
    _succeeded_job,
    set_rule_data,
)
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
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    ExtensionConstraint,
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
        "GO:0008150": TermFact(
            TermStatus.CURRENT, checked_ancestors=frozenset({"GO:0008150"})
        ),
        "CL:0000099": TermFact(TermStatus.ALTERNATE, primary_id="CL:0000000"),
        "CL:9999999": TermFact(TermStatus.UNKNOWN),
        "GO:0000003": TermFact(TermStatus.OBSOLETE, replaced_by=("GO:0008150",)),
    }
    assert first.entity == EntityFact("NCBITaxon:7955", "PR:000000001")
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

    assert facts.terms["GO:0008150"] == TermFact(
        TermStatus.CURRENT, checked_ancestors=frozenset({"GO:0008150"})
    )


def test_go_facts_include_subsets_checked_ancestors_rule_data_and_retracted_pmids(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
    reference_data: Callable[..., None],
) -> None:
    """GO terms and entity types come with the checked ancestors they fall under.

    Checked ancestors include the extension constraints' primary root terms. The facts
    also carry the active rule data and the references listed as retracted.
    """
    seed_active_subjects("ComplexPortal:CPX-1", db_object_type="GO:0032991")
    reference_data(
        SeedTerm(
            "GO:0099991",
            parents=("GO:0006915",),
            subsets=("gocheck_obsoletion_candidate",),
        )
    )
    rule_data = replace(
        DEFAULT_RULE_DATA,
        extension_constraints=(
            ExtensionConstraint("BFO:0000050", ("CL",), ("GO:0006915",)),
        ),
    )
    set_rule_data(session_factory, rule_data, retracted_pmids=("PMID:9",))
    annotation = _annotation(
        db_object_id="ComplexPortal:CPX-1",
        ontology_class_id="GO:0099991",
        references=["PMID:9", "PMID:1"],
    )

    with unit_of_work_factory() as uow:
        (facts,) = uow.validation_facts.gather([annotation], [()], ONTOLOGIES)

    assert facts.terms["GO:0099991"] == TermFact(
        TermStatus.CURRENT,
        subsets=frozenset({"gocheck_obsoletion_candidate"}),
        checked_ancestors=frozenset({"GO:0006915", "GO:0008150"}),
    )
    assert facts.entity == EntityFact(
        "NCBITaxon:7955",
        "GO:0032991",
        type_checked_ancestors=frozenset({"GO:0005575", "GO:0032991"}),
    )
    assert facts.rule_data == rule_data
    assert facts.retracted_pmids == frozenset({"PMID:9"})


def test_replacement_facts_follow_chains_to_terms_the_annotation_does_not_use(
    unit_of_work_factory: UnitOfWorkFactory,
    reference_data: Callable[..., None],
) -> None:
    """Facts include every term a chain reaches, and the snapshot IDs read."""
    reference_data(
        SeedTerm("GO:0000011", obsolete=True, replaced_by=("GO:0000012",)),
        SeedTerm("GO:0000012", obsolete=True, replaced_by=("GO:0000013",)),
        SeedTerm("GO:0000013", parents=("GO:0008150",)),
    )
    annotation = _annotation(ontology_class_id="GO:0000011")

    with unit_of_work_factory() as uow:
        facts = uow.validation_facts.replacement_facts([annotation], ONTOLOGIES)
        go_version = uow.ontologies.get_active("go")
        assert go_version is not None
        go_version_id = go_version.version_id

    assert facts.terms["GO:0000011"].replaced_by == ("GO:0000012",)
    assert facts.terms["GO:0000012"].status is TermStatus.OBSOLETE
    assert facts.terms["GO:0000013"].status is TermStatus.CURRENT
    assert facts.snapshot_ids["go"] == go_version_id
    assert set(facts.snapshot_ids) == set(ONTOLOGIES) | {"rules"}
