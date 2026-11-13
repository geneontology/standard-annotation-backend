"""Verify GORULE:0000007: IPI cannot support catalytic activity terms."""

from gorule_cases import annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    CATALYTIC_ACTIVITY,
    MF_ROOT,
)

rule = go_rule("gorule_0000007")

KINASE = "GO:0004672"


def test_ipi_on_a_catalytic_activity_term_is_an_issue() -> None:
    """IPI on a term under `GO:0003824` is reported."""
    subject = annotation(
        "IPI",
        ontology_class_id=KINASE,
        relation="RO:0002327",
        with_or_from=["UniProtKB:Q1"],
    )

    issues = rule(subject, facts({KINASE: term(MF_ROOT, CATALYTIC_ACTIVITY)}))

    assert found(issues) == [("gorule_0000007", ("annotation",))]


def test_ipi_on_another_function_and_ida_on_catalysis_pass() -> None:
    """IPI elsewhere in MF, and other evidence on catalysis, pass."""
    ipi = annotation(
        "IPI",
        ontology_class_id=KINASE,
        relation="RO:0002327",
        with_or_from=["UniProtKB:Q1"],
    )
    ida = annotation("IDA", ontology_class_id=KINASE, relation="RO:0002327")

    assert found(rule(ipi, facts({KINASE: term(MF_ROOT)}))) == []
    catalytic = facts({KINASE: term(MF_ROOT, CATALYTIC_ACTIVITY)})
    assert found(rule(ida, catalytic)) == []
