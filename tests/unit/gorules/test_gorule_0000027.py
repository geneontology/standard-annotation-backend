"""Verify GORULE:0000027: identifiers name known databases, in their syntax,
and `assigned_by` names a known group."""

from dataclasses import replace

from gorule_cases import RULE_DATA, annotation, facts, found, go_rule

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    DatabaseEntry,
)

rule = go_rule("gorule_0000027")

DATA = replace(
    RULE_DATA,
    databases=(
        DatabaseEntry("CL"),
        DatabaseEntry("MGI", id_syntaxes=("MGI:[0-9]{5,}",)),
        DatabaseEntry("UniProtKB", id_syntaxes=("[A-Z][0-9][A-Z0-9]{3}[0-9]",)),
    ),
    group_shorthands=("GO_Central", "MGI"),
)


def test_unknown_prefixes_are_reported_at_each_identifier() -> None:
    """Prefixes must match a database exactly, including case, in every field."""
    subject = annotation(
        with_or_from=["uniprotkb:Q12345", "FlyBase:FBgn0000001"],
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "UBERON:0000001"}
        ],
    )

    assert found(rule(subject, facts(rule_data=DATA))) == [
        ("gorule_0000027", ("annotation", "with_or_from", 0)),
        ("gorule_0000027", ("annotation", "with_or_from", 1)),
        (
            "gorule_0000027",
            ("annotation", "annotation_extensions", 0, "extension_term"),
        ),
    ]


def test_a_local_identifier_must_match_one_of_the_databases_syntaxes() -> None:
    """A local identifier that fails the syntax is reported; one that fits passes."""
    wrong = annotation(db_object_id="UniProtKB:not-an-accession")
    fits = annotation(
        db_object_id="MGI:MGI:12345",
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "CL:anything"}
        ],
    )

    assert found(rule(wrong, facts(rule_data=DATA))) == [
        ("gorule_0000027", ("annotation", "db_object_id"))
    ]
    assert found(rule(fits, facts(rule_data=DATA))) == []


def test_assigned_by_must_be_a_group_shorthand() -> None:
    """An unknown group is reported at `assigned_by`; nothing fires without rule data."""
    subject = annotation(assigned_by="Unknown_Group")

    assert found(rule(subject, facts(rule_data=DATA))) == [
        ("gorule_0000027", ("annotation", "assigned_by"))
    ]
    assert found(rule(subject, facts(rule_data=None))) == []
