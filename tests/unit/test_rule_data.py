"""Verify the pure checks over go-site metadata."""

from typing import cast

import pytest

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    DatabaseEntry,
    ExtensionConstraint,
    RuleData,
    RuleDataIndex,
    unrecognized_extension_namespaces,
)


def _data(*namespace_lists: tuple[str, ...]) -> RuleData:
    """Return rule data with two databases and one extension entry per list."""
    return RuleData(
        databases=(DatabaseEntry(database="MGI"), DatabaseEntry(database="CHEBI")),
        go_refs=(),
        group_shorthands=(),
        eco_usage_constraints=(),
        extension_constraints=tuple(
            ExtensionConstraint(
                relation_id="BFO:0000050",
                namespaces=namespaces,
                primary_root_terms=("GO:0008150",),
            )
            for namespaces in namespace_lists
        ),
    )


def test_only_namespaces_neither_configured_nor_databases_are_reported() -> None:
    """A namespace that a configured ontology owns or `db-xrefs.yaml` lists is known.

    Every other namespace is reported once, in sorted order, however many entries
    list it.
    """
    data = _data(("GO", "MGI", "XAO"), ("ZFA", "CHEBI", "XAO"), ("FBbt",))

    assert unrecognized_extension_namespaces(data, {"GO", "ZFA"}) == ("FBbt", "XAO")


def test_indexed_rule_data_cannot_be_changed() -> None:
    """Rule data shared between validations is read-only, and is not hashable."""
    index = RuleDataIndex.of(_data(("GO",)))
    # A caller that ignores the `Mapping` type and writes to the lookup anyway.
    databases = cast("dict[str, DatabaseEntry]", index.databases)

    with pytest.raises(TypeError):
        databases["XAO"] = DatabaseEntry(database="XAO")
    with pytest.raises(TypeError):
        hash(index)
    assert set(index.databases) == {"MGI", "CHEBI"}
