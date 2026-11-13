"""Verify parsing of the go-site metadata files that GO Rules read."""

from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    DatabaseEntry,
    EcoUsageConstraint,
    EntityIsComplex,
    ExtensionConstraint,
    GoRefEntry,
    ReferenceMatches,
    RuleData,
    WithPresence,
    WithPresenceCondition,
)
from standard_annotation_backend.go_site.rule_files import (
    DB_XREFS_PATH,
    GOREFS_PATH,
    GROUPS_PATH,
    RULE_FILE_PATHS,
    InvalidRuleFileError,
    parse_db_xrefs,
    parse_eco_usage_constraints,
    parse_extensions_constraints,
    parse_gorefs,
    parse_groups,
    parse_retracted_publications,
    parse_rule_files,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "go_site"
EXPECTED = RuleData(
    databases=(
        DatabaseEntry(database="GO", id_syntaxes=(r"\d{7}",)),
        DatabaseEntry(database="MGI", id_syntaxes=("MGI:[0-9]+",)),
        DatabaseEntry(database="PMID", id_syntaxes=("[0-9]+",)),
        DatabaseEntry(database="CHEBI", id_syntaxes=("[0-9]{1,6}",)),
        DatabaseEntry(database="GOC"),
    ),
    go_refs=(
        GoRefEntry(go_ref_id="GO_REF:0000015", alt_ids=("GO_REF:0000005",)),
        GoRefEntry(go_ref_id="GO_REF:0000033"),
        GoRefEntry(go_ref_id="GO_REF:0000001", obsolete=True),
    ),
    group_shorthands=("GO_Central", "MGI"),
    eco_usage_constraints=(
        EcoUsageConstraint(
            eco_id="ECO:0000353",
            allowed_reference="PMID:[0-9]+",
            with_presence=WithPresence.CONDITIONAL,
            condition=WithPresenceCondition(
                test=EntityIsComplex(),
                when_true=WithPresence.OPTIONAL,
                when_false=WithPresence.MANDATORY,
            ),
        ),
        EcoUsageConstraint(
            eco_id="ECO:0000320",
            allowed_reference="(GO_REF:0000(033|047))|(PMID:[0-9]+)",
            with_presence=WithPresence.CONDITIONAL,
            condition=WithPresenceCondition(
                test=ReferenceMatches(pattern="^GO_REF:[0-9]{7}$"),
                when_true=WithPresence.MANDATORY,
                when_false=WithPresence.OPTIONAL,
            ),
        ),
        EcoUsageConstraint(
            eco_id="ECO:0000250",
            allowed_reference="(GO_REF:0000(024|027|110|122))|(PMID:[0-9]+)",
            with_presence=WithPresence.MANDATORY,
        ),
        EcoUsageConstraint(
            eco_id="ECO:0000501",
            allowed_reference=None,
            with_presence=WithPresence.MANDATORY,
        ),
    ),
    extension_constraints=(
        ExtensionConstraint(
            relation_id="BFO:0000066",
            namespaces=("GO", "CHEBI"),
            primary_root_terms=("GO:0003674", "GO:0008150"),
            cardinality=1,
        ),
        ExtensionConstraint(
            relation_id="RO:0002233",
            namespaces=("MGI", "CHEBI"),
            primary_root_terms=("GO:0003674",),
        ),
        ExtensionConstraint(
            relation_id="BFO:0000050",
            namespaces=("GO",),
            primary_root_terms=("GO:0008150",),
        ),
    ),
)


def _texts() -> dict[str, str]:
    """Return each fixture file's text, keyed by its path in the go-site repository."""
    return {
        path: (FIXTURES / PurePosixPath(path).name).read_text(encoding="utf-8")
        for path in RULE_FILE_PATHS
    }


def _eco(entry: str) -> str:
    """Return an `eco-usage-constraints.yaml` document whose one entry has `entry`."""
    return "eco_usage_constraints:\n  - eco_id: ECO:0000353\n" + entry


def _conditional(condition: str, when_true: str = "optional") -> str:
    """Return the fields of a conditional evidence entry."""
    return (
        "    with_presence: conditional\n"
        "    with_presence_condition:\n"
        f"      condition: {condition}\n"
        f"      when_true: {when_true}\n"
        "      when_false: mandatory\n"
    )


def _extension(field: str) -> str:
    """Return an `extensions-constraints.yaml` document with one entry and `field`."""
    return (
        "- relation: occurs_in\n"
        "  relation_id: BFO:0000066\n"
        "  namespaces: [GO]\n"
        "  primary_root_terms: [GO:0008150]\n"
        f"  {field}\n"
    )


def test_published_files_parse_into_the_values_the_rules_use() -> None:
    """Each file keeps only the values GO Rules check.

    The fixtures follow the published files. They include fields SABE does not read,
    YAML anchors, comment lines, PMIDs followed by a PMCID, an evidence entry with an
    extra `allowed_references` list, and a namespace listed twice in one entry. The
    extension entry without a `relation_id` is left out, because no annotation can
    match it.
    """
    files = parse_rule_files(_texts())

    assert files.data == EXPECTED
    assert files.retracted_pmids == {
        "PMID:10026173",
        "PMID:10047653",
        "PMID:10066663",
    }


def test_rule_data_reads_back_unchanged_from_its_stored_form() -> None:
    """Rule data written as JSON reads back equal, including both condition kinds."""
    assert STORED_RULE_DATA.load(STORED_RULE_DATA.dump(EXPECTED)) == EXPECTED


INVALID = [
    pytest.param(
        parse_db_xrefs,
        "- database: GO\n- database: GO\n",
        (1, "database"),
        "duplicate_entry",
        id="repeated-database",
    ),
    pytest.param(
        parse_db_xrefs,
        "- database: 'GO:REF'\n",
        (0, "database"),
        "string_pattern_mismatch",
        id="database-with-colon",
    ),
    pytest.param(
        parse_db_xrefs,
        "- database: GO\n  entity_types:\n    - id_syntax: '[0-9'\n",
        (0, "entity_types", 0, "id_syntax"),
        "invalid_pattern",
        id="uncompilable-id-syntax",
    ),
    pytest.param(parse_db_xrefs, "database: GO\n", (), "tuple_type", id="not-a-list"),
    pytest.param(
        parse_db_xrefs,
        "- database: GO\n  name: a\n  name: b\n",
        (),
        "invalid_yaml",
        id="repeated-key",
    ),
    pytest.param(
        parse_gorefs,
        "- id: GO_REF:15\n",
        (0, "id"),
        "string_pattern_mismatch",
        id="malformed-go-ref",
    ),
    pytest.param(
        parse_gorefs,
        "- id: GO_REF:0000001\n- id: GO_REF:0000001\n",
        (1, "id"),
        "duplicate_entry",
        id="repeated-go-ref",
    ),
    pytest.param(
        parse_gorefs,
        "- id: GO_REF:0000001\n  alt_id: [GO_REF:5]\n",
        (0, "alt_id", 0),
        "string_pattern_mismatch",
        id="malformed-alt-id",
    ),
    pytest.param(
        parse_groups, "- label: MGI\n", (0, "shorthand"), "missing", id="no-shorthand"
    ),
    pytest.param(
        parse_groups,
        "- shorthand: MGI\n- shorthand: MGI\n",
        (1, "shorthand"),
        "duplicate_entry",
        id="repeated-shorthand",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco(_conditional("db_object_type eq 'protein'")),
        ("eco_usage_constraints", 0, "with_presence_condition", "condition"),
        "unsupported_condition",
        id="unsupported-condition",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco(_conditional("reference matches '(['")),
        ("eco_usage_constraints", 0, "with_presence_condition", "condition"),
        "invalid_pattern",
        id="uncompilable-condition-pattern",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco(_conditional("db_object_type eq 'complex'", when_true="conditional")),
        ("eco_usage_constraints", 0, "with_presence_condition"),
        "nested_condition",
        id="conditional-outcome",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco("    with_presence: conditional\n"),
        ("eco_usage_constraints", 0),
        "condition_mismatch",
        id="conditional-without-condition",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco(
            "    with_presence: mandatory\n"
            "    with_presence_condition:\n"
            "      condition: db_object_type eq 'complex'\n"
            "      when_true: optional\n"
            "      when_false: mandatory\n"
        ),
        ("eco_usage_constraints", 0),
        "condition_mismatch",
        id="condition-without-conditional",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco("    with_presence: sometimes\n"),
        ("eco_usage_constraints", 0, "with_presence"),
        "enum",
        id="unknown-with-presence",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco("    with_presence: mandatory\n    allowed_reference: 'PMID:(['\n"),
        ("eco_usage_constraints", 0, "allowed_reference"),
        "invalid_pattern",
        id="uncompilable-allowed-reference",
    ),
    pytest.param(
        parse_eco_usage_constraints,
        _eco("    with_presence: mandatory\n")
        + "  - eco_id: ECO:0000353\n    with_presence: optional\n",
        ("eco_usage_constraints", 1, "eco_id"),
        "duplicate_entry",
        id="repeated-eco-id",
    ),
    pytest.param(
        parse_extensions_constraints,
        _extension("cardinality: 0"),
        (0, "cardinality"),
        "greater_than_equal",
        id="zero-cardinality",
    ),
    pytest.param(
        parse_extensions_constraints,
        _extension("cardinality: true"),
        (0, "cardinality"),
        "int_type",
        id="boolean-cardinality",
    ),
    pytest.param(
        parse_extensions_constraints,
        "- relation: occurs_in\n  namespaces: []\n  primary_root_terms: [GO:0008150]\n",
        (0, "namespaces"),
        "too_short",
        id="no-namespaces",
    ),
    pytest.param(
        parse_extensions_constraints,
        "- relation: occurs_in\n  namespaces: [GO]\n  primary_root_terms: [CL:0000000]\n",
        (0, "primary_root_terms", 0),
        "string_pattern_mismatch",
        id="root-not-in-go",
    ),
    pytest.param(
        parse_extensions_constraints,
        "- relation: occurs_in\n"
        "  relation_id: RO_0002233\n"
        "  namespaces: [GO]\n"
        "  primary_root_terms: [GO:0008150]\n",
        (0, "relation_id"),
        "string_pattern_mismatch",
        id="malformed-relation-id",
    ),
]


@pytest.mark.parametrize(("parser", "text", "location", "issue_type"), INVALID)
def test_malformed_values_reject_the_file_at_their_location(
    parser: Callable[[str], object],
    text: str,
    location: tuple[str | int, ...],
    issue_type: str,
) -> None:
    """A value the rules rely on that is malformed or repeated rejects its file.

    The issue gives the value's location within the file.
    """
    with pytest.raises(InvalidRuleFileError) as raised:
        parser(text)

    assert (location, issue_type) in [
        (issue["location"], issue["type"]) for issue in raised.value.issues
    ]


def test_each_retracted_publication_line_without_a_pmid_is_reported() -> None:
    """Lines that do not start with a PMID are reported by line number.

    Comment lines starting with `!` and blank lines are skipped.
    """
    text = "! comment\nPMID:1\nPMCID:PMC2\n\nPMID:x,PMCID:PMC3\n"

    with pytest.raises(InvalidRuleFileError) as raised:
        parse_retracted_publications(text)

    assert [(issue["location"], issue["type"]) for issue in raised.value.issues] == [
        ((3,), "invalid_pmid"),
        ((5,), "invalid_pmid"),
    ]


def test_problems_in_every_file_are_reported_together_under_its_path() -> None:
    """Each invalid or missing file is reported at once, located by its path."""
    texts = _texts()
    texts[DB_XREFS_PATH] = "- database: GO\n  name: a\n  name: b\n"
    texts[GOREFS_PATH] = "- id: GO_REF:15\n"
    del texts[GROUPS_PATH]

    with pytest.raises(InvalidRuleFileError) as raised:
        parse_rule_files(texts)

    assert [(issue["location"], issue["type"]) for issue in raised.value.issues] == [
        ((DB_XREFS_PATH,), "invalid_yaml"),
        ((GOREFS_PATH, 0, "id"), "string_pattern_mismatch"),
        ((GROUPS_PATH,), "missing_file"),
    ]
