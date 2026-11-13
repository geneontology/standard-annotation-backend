"""Verify GORULE:0000053: negated annotations cannot have extensions."""

from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000053")


EXTENSION = [{"extension_relation": "BFO:0000066", "extension_term": "CL:0000000"}]


def test_a_negated_annotation_with_an_extension_is_an_issue() -> None:
    """Negation and extensions together are reported."""
    subject = annotation(negation=True, annotation_extensions=EXTENSION)

    assert found(rule(subject, facts())) == [("gorule_0000053", ("annotation",))]


def test_negation_or_extensions_alone_pass() -> None:
    """Either one without the other passes."""
    assert found(rule(annotation(negation=True), facts())) == []
    extended = annotation(annotation_extensions=EXTENSION)
    assert found(rule(extended, facts())) == []
