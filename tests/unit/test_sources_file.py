"""Verify validation of the reference data sources file."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
)
from standard_annotation_backend.refresh.sources import (
    GitHubEntitySource,
    GitHubSource,
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
GO = {
    "type": "github",
    "repository": "geneontology/go-ontology",
    "ref": "master",
    "path": "src/ontology/go-edit.obo",
}


def _file(**sections: object) -> dict[str, object]:
    """Return a valid sources mapping with the given sections replaced."""
    return {"authorization": GO_SITE, "ontologies": {"go": GO}, **sections}


def test_shipped_sources_file_is_valid() -> None:
    """The committed file configures go-site, GO, and the entity sources."""
    sources = RefreshSources(load_sources_file(SHIPPED_FILE))

    assert sources.keys(RefreshKindName.AUTHORIZATION) == ("go-site",)
    assert sources.keys(RefreshKindName.ONTOLOGY) == ("go",)
    assert sources.keys(RefreshKindName.ENTITY) == ("caeel", "mouse")


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
