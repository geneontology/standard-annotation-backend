"""Verify immutable retrieval through the GitHub ontology source adapter."""

from hashlib import sha256

import httpx2
import pytest

from standard_annotation_backend.domain.ontology import OntologyKey
from standard_annotation_backend.ontology.sources import (
    GitHubOntologySource,
    OntologySourceError,
)

SHA = "a" * 40
OBO = b"format-version: 1.4\nontology: go\n"


def _client(
    responses: list[httpx2.Response | Exception],
) -> tuple[httpx2.Client, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler)), requests


def _source(client: httpx2.Client) -> GitHubOntologySource:
    return GitHubOntologySource(
        ontology_key=OntologyKey.GO,
        repository="geneontology/go-ontology",
        ref="master",
        path="src/ontology/go-edit.obo",
        client=client,
    )


def test_fetch_resolves_ref_and_reads_exact_bytes_at_immutable_sha() -> None:
    """The retrieved document records a commit instead of the mutable GitHub ref."""
    client, requests = _client(
        [
            httpx2.Response(200, json={"sha": SHA.upper()}),
            httpx2.Response(200, content=OBO),
        ]
    )

    document = _source(client).fetch()

    assert document.ontology_key is OntologyKey.GO
    assert document.content == OBO
    assert document.source_type == "github"
    assert document.source_locator == (
        "github:geneontology/go-ontology:src/ontology/go-edit.obo"
    )
    assert document.source_revision == SHA
    assert document.source_checksum == sha256(OBO).hexdigest()
    assert len(requests) == 2
    assert str(requests[0].url) == (
        "https://api.github.com/repos/geneontology/go-ontology/commits/master"
    )
    assert str(requests[1].url) == (
        "https://raw.githubusercontent.com/geneontology/go-ontology/"
        f"{SHA}/src/ontology/go-edit.obo"
    )


@pytest.mark.parametrize(
    "responses",
    [
        [httpx2.Response(404, content=b"secret commit response")],
        [httpx2.Response(200, content=b"not-json")],
        [httpx2.Response(200, json={})],
        [httpx2.Response(200, json={"sha": "a" * 39})],
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(503, content=b"private source response"),
        ],
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(200, content=b""),
        ],
    ],
)
def test_fetch_returns_one_safe_error_for_invalid_upstream_responses(
    responses: list[httpx2.Response],
) -> None:
    """Malformed and failed upstream responses expose no response details."""
    client, _requests = _client(list(responses))

    with pytest.raises(
        OntologySourceError,
        match=r"^Configured ontology source is unavailable$",
    ) as caught:
        _source(client).fetch()

    assert "secret" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_fetch_returns_safe_error_for_network_failure() -> None:
    """Network diagnostics do not escape the source adapter."""
    request = httpx2.Request("GET", "https://api.github.com")
    client, _requests = _client(
        [httpx2.ConnectError("credential-like diagnostic", request=request)]
    )

    with pytest.raises(OntologySourceError) as caught:
        _source(client).fetch()

    assert "credential-like" not in str(caught.value)
