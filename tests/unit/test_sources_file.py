"""Verify validation of the reference data sources file."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from standard_annotation_backend.domain.ontology import OntologyDefinition
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    UnknownSourceError,
)
from standard_annotation_backend.refresh.sources import (
    GitHubAnnotationSource,
    GitHubEntitySource,
    GitHubRulesSource,
    GitHubSource,
    HttpsAnnotationSource,
    HttpsEntitySource,
    HttpsSource,
    RefreshSources,
    SourcesFile,
    load_sources_file,
)

SHIPPED_FILE = Path(__file__).parents[2] / "config" / "sources.yaml"
GO_SITE = {
    "type": "github",
    "repository": "geneontology/go-site",
    "ref": "master",
    "path": "metadata/users.yaml",
}
RULES = {"type": "github", "repository": "geneontology/go-site", "ref": "master"}
GO = {
    "type": "github",
    "repository": "geneontology/go-ontology",
    "ref": "master",
    "path": "src/ontology/go-edit.obo",
    "document_ontology_id": "go",
    "prefixes": ["GO"],
    "closure_predicates": ["rdfs:subClassOf", "BFO:0000050"],
    "required_subsets": ["gocheck_do_not_annotate", "gocheck_obsoletion_candidate"],
}
CL = {
    "type": "https",
    "url": "https://example.org/cl-basic.obo",
    "document_ontology_id": "cl/cl-basic",
    "prefixes": ["CL"],
}


def _file(**sections: object) -> dict[str, object]:
    """Return a valid sources mapping with the given sections replaced."""
    return {
        "authorization": GO_SITE,
        "rules": RULES,
        "ontologies": {"go": GO},
        **sections,
    }


def test_shipped_sources_file_is_valid() -> None:
    """The committed sources file loads with the go-site and ontology sources it ships.

    GO is read with its expected `ontology: go` header.
    """
    sources = RefreshSources(load_sources_file(SHIPPED_FILE))

    assert sources.keys(RefreshKindName.AUTHORIZATION) == ("go-site",)
    assert sources.keys(RefreshKindName.RULES) == ("go-site",)
    assert sources.source(RefreshKindName.RULES, "go-site") == GitHubRulesSource(
        type="github", repository="geneontology/go-site", ref="master"
    )
    assert sources.keys(RefreshKindName.ONTOLOGY) == (
        "chebi",
        "cl",
        "emapa",
        "fbbt",
        "go",
        "ncbitaxon",
        "pr",
        "so",
        "uberon",
        "wbbt",
        "wbls",
        "xao",
        "zfa",
    )
    assert sources.ontology_definition("go").document_ontology_id == "go"


def test_any_kind_may_use_either_source_type() -> None:
    """Authorization may come from HTTPS and an entity source from GitHub."""
    sources = RefreshSources(
        SourcesFile.model_validate(
            _file(
                authorization={"type": "https", "url": "https://example.org/u.yaml"},
                entities={
                    "mgi": {
                        "type": "github",
                        "repository": "org/repo",
                        "ref": "main",
                        "path": "gpi/mgi.gpi",
                    },
                    "rgd": {"type": "https", "url": "https://example.org/rgd.gpi"},
                },
            )
        )
    )

    assert isinstance(
        sources.source(RefreshKindName.AUTHORIZATION, "go-site"), HttpsSource
    )
    mgi = sources.source(RefreshKindName.ENTITY, "mgi")
    rgd = sources.source(RefreshKindName.ENTITY, "rgd")
    assert isinstance(mgi, GitHubEntitySource) and mgi.format == "gpi"
    assert isinstance(rgd, HttpsEntitySource) and rgd.format == "gpi"
    assert isinstance(sources.source(RefreshKindName.ONTOLOGY, "go"), GitHubSource)


def test_each_ontology_is_read_as_its_entry_describes() -> None:
    """An ontology entry sets the header to expect, the prefixes, and the closure."""
    sources = RefreshSources(
        SourcesFile.model_validate(_file(ontologies={"go": GO, "cl": CL}))
    )

    assert sources.ontology_definition("cl") == OntologyDefinition(
        key="cl",
        identifier_prefixes=("CL",),
        closure_predicates=(),
        document_ontology_id="cl/cl-basic",
    )
    assert sources.ontology_definition("go").closure_predicates == (
        "rdfs:subClassOf",
        "BFO:0000050",
    )


def test_an_unconfigured_ontology_has_no_definition() -> None:
    """Asking how to read an ontology the file does not configure fails."""
    sources = RefreshSources(SourcesFile.model_validate(_file()))

    with pytest.raises(UnknownSourceError):
        sources.ontology_definition("cl")


@pytest.mark.parametrize(
    "ontologies",
    [
        {"cl": CL},
        {"go": {**GO, "closure_predicates": ["BFO:0000050"]}},
        {"go": {**GO, "required_subsets": ["gocheck_do_not_annotate"]}},
        {"go": GO, "cl": {**CL, "prefixes": []}},
        {"go": GO, "cl": {**CL, "prefixes": ["GO"]}},
        {
            "go": {
                key: value for key, value in GO.items() if key != "document_ontology_id"
            }
        },
    ],
    ids=[
        "no-go",
        "go-without-subclass-closure",
        "go-without-rule-subsets",
        "no-prefixes",
        "shared-prefix",
        "no-expected-header",
    ],
)
def test_ontology_entries_sabe_cannot_rely_on_are_rejected(
    ontologies: dict[str, object],
) -> None:
    """Entries that would leave GO or an identifier prefix unusable are rejected.

    GO needs subclass closure and the subsets GO Rules check, and every prefix
    needs exactly one ontology.
    """
    with pytest.raises(ValidationError):
        SourcesFile.model_validate(_file(ontologies=ontologies))


@pytest.mark.parametrize(
    "entry",
    [
        {**CL, "prefixes": ["CL", "CL"]},
        {**CL, "closure_predicates": ["rdfs:subClassOf", "rdfs:subClassOf"]},
        {**CL, "required_subsets": ["cellxgene_subset", "cellxgene_subset"]},
    ],
    ids=["repeated-prefix", "repeated-closure-predicate", "repeated-required-subset"],
)
def test_an_ontology_entry_may_not_repeat_a_value(
    entry: dict[str, object],
) -> None:
    """A value listed twice in one ontology entry is rejected at startup."""
    with pytest.raises(ValidationError, match="the same value twice"):
        SourcesFile.model_validate(_file(ontologies={"go": GO, "cl": entry}))


@pytest.mark.parametrize("section", ["ontologies", "entities"])
def test_null_entity_section_is_empty_but_go_stays_required(section: str) -> None:
    """A null entities section is empty; a null ontologies section lacks GO."""
    data = _file(**{section: None})
    if section == "entities":
        sources = RefreshSources(SourcesFile.model_validate(data))
        assert sources.keys(RefreshKindName.ENTITY) == ()
    else:
        with pytest.raises(ValidationError, match="must configure the GO ontology"):
            SourcesFile.model_validate(data)


@pytest.mark.parametrize(
    "data",
    [
        {"ontologies": {"go": GO}},
        _file(ontologies={}),
        _file(ontologies={"go": GO, "chebi": GO}),
        _file(authorization={"repository": "o/r", "ref": "m", "path": "u.yaml"}),
        _file(authorization={"type": "ftp", "url": "ftp://example.org/u.yaml"}),
        _file(authorization={"type": "https", "url": "http://example.org/u.yaml"}),
        _file(authorization={**GO_SITE, "path": "../users.yaml"}),
        _file(authorization={**GO_SITE, "path": "/metadata/users.yaml"}),
        _file(authorization={**GO_SITE, "repository": "go-site"}),
        _file(authorization={**GO_SITE, "ref": "  "}),
        _file(authorization={**GO_SITE, "unexpected": True}),
        {key: value for key, value in _file().items() if key != "rules"},
        _file(rules={"type": "https", "url": "https://example.org/metadata"}),
        _file(rules={**RULES, "path": "metadata/gorefs.yaml"}),
        _file(rules={**RULES, "repository": "go-site"}),
        _file(rules={**RULES, "ref": "  "}),
        _file(entities={"Mouse": {"type": "https", "url": "https://e.org/m"}}),
        _file(
            entities={
                "mouse": {"type": "https", "url": "https://e.org/m", "format": "gaf"}
            }
        ),
        _file(entities=["mouse"]),
        _file(unexpected={}),
    ],
)
def test_invalid_sources_are_rejected(data: dict[str, object]) -> None:
    """Every malformed or unsafe entry is rejected before any process starts."""
    with pytest.raises(ValidationError):
        SourcesFile.model_validate(data)


@pytest.mark.parametrize("text", ["", "authorization: [", "- go-site\n"])
def test_unreadable_or_malformed_file_is_rejected(tmp_path: Path, text: str) -> None:
    """Empty, unparsable, and non-mapping files are rejected."""
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_sources_file(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    """A missing file is rejected with a message that names no path."""
    with pytest.raises(ValueError, match=r"^sources file could not be read$"):
        load_sources_file(tmp_path / "missing.yaml")


MGI_GPAD = {"type": "https", "group": "MGI", "url": "https://example.org/mgi.gpad"}


def test_annotation_sources_name_their_owning_group() -> None:
    """An annotation source may use either type and names its annotations' group."""
    sources = RefreshSources(
        SourcesFile.model_validate(
            _file(
                annotations={
                    "mgi": MGI_GPAD,
                    "go-central": {
                        "type": "github",
                        "group": "GO_Central",
                        "repository": "geneontology/example",
                        "ref": "main",
                        "path": "annotations/go_central.gpad",
                    },
                }
            )
        )
    )

    assert sources.keys(RefreshKindName.ANNOTATION) == ("go-central", "mgi")
    assert isinstance(
        sources.source(RefreshKindName.ANNOTATION, "mgi"), HttpsAnnotationSource
    )
    assert isinstance(
        sources.source(RefreshKindName.ANNOTATION, "go-central"), GitHubAnnotationSource
    )
    assert sources.annotation_group("go-central") == "GO_Central"
    mgi = sources.source(RefreshKindName.ANNOTATION, "mgi")
    assert isinstance(mgi, HttpsAnnotationSource)
    assert mgi.format == "gpad"
    with pytest.raises(UnknownSourceError):
        sources.annotation_group("zfin")


def test_null_annotation_section_is_empty() -> None:
    """An `annotations:` key with no value configures no annotation sources."""
    sources = RefreshSources(SourcesFile.model_validate(_file(annotations=None)))

    assert sources.keys(RefreshKindName.ANNOTATION) == ()


@pytest.mark.parametrize(
    "annotations",
    [
        {"mgi": {"type": "https", "url": "https://example.org/mgi.gpad"}},
        {"mgi": {**MGI_GPAD, "group": "  "}},
        {"mgi": {**MGI_GPAD, "format": "gaf"}},
        {"MGI": MGI_GPAD},
        {
            "mgi": MGI_GPAD,
            "mgi-copy": {**MGI_GPAD, "url": "https://example.org/b.gpad"},
        },
    ],
    ids=[
        "missing-group",
        "blank-group",
        "unsupported-format",
        "bad-key",
        "shared-group",
    ],
)
def test_invalid_annotation_sources_are_rejected(annotations: object) -> None:
    """A source needs a valid key, a nonblank group no other source uses, and GPAD."""
    with pytest.raises(ValidationError):
        SourcesFile.model_validate(_file(annotations=annotations))


def test_a_repeated_key_is_rejected_instead_of_replacing_the_first(
    tmp_path: Path,
) -> None:
    """A section listed twice fails to load, even when both copies are valid."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        SHIPPED_FILE.read_text(encoding="utf-8")
        + "\nrules:\n  type: github\n  repository: geneontology/go-site\n"
        "  ref: master\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"^sources file could not be read$"):
        load_sources_file(path)
