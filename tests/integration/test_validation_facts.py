"""Verify the facts semantic validation reads from the active snapshots."""

from collections.abc import Callable
from dataclasses import replace
from uuid import UUID

from seeding import DEFAULT_RULE_DATA, SHIPPED_SOURCES, SeedTerm, set_rule_data
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.facts import (
    EntityFact,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    ExtensionConstraint,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory

ONTOLOGIES = SHIPPED_SOURCES.ontology_catalog()
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
