"""Shared pytest fixtures for the SAB test suite."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.main import app


@pytest.fixture
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide complete test configuration, clearing cached settings between tests."""
    monkeypatch.setenv("SAB_DATABASE_URL", "postgresql+psycopg://sab:sab@db:5432/sab")
    monkeypatch.setenv("SAB_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SAB_APPLICATION_SECRET", "test-application-secret")
    monkeypatch.setenv("SAB_ENVIRONMENT", "test")
    monkeypatch.setenv("SAB_LOG_FORMAT", "console")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def client(configured_environment: None) -> Iterator[TestClient]:
    """Run the FastAPI application with valid test configuration."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def validated_annotation() -> Annotation:
    """Create a valid annotation for persistence tests.

    Returns:
        A validated annotation containing repeated and unordered list values.
    """
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "negation": None,
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:2", "PMID:1", "PMID:1"],
            "evidence_type": "ECO:0000314",
            "with_or_from": ["UniProtKB:Q2", "UniProtKB:Q1", "UniProtKB:Q1"],
            "interacting_taxon_id": [
                "NCBITaxon:10090",
                "NCBITaxon:9606",
                "NCBITaxon:9606",
            ],
            "annotation_date": "2026-09-09",
            "assigned_by": "GO_Central",
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000000",
                }
            ],
            "annotation_properties": {"comment": ["excluded"]},
        }
    )
