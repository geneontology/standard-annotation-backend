"""Build source provenance values shared by integration tests."""

from datetime import UTC, datetime

from standard_annotation_backend.domain.refresh import SourceProvenance


def github_provenance(
    revision: str,
    repository: str = "geneontology/go-site",
    *,
    path: str = "metadata/users.yaml",
) -> SourceProvenance:
    """Return provenance for a GitHub document at an exact commit.

    The checksum is derived from the revision, so different revisions describe
    different documents.

    Args:
        revision: Commit SHA of the document.
        repository: Repository that holds the document.
        path: Path of the document in the repository.
    """
    return SourceProvenance(
        source_type="github",
        source_locator=f"github:{repository}:{path}",
        source_revision=revision,
        source_checksum=revision[0] * 64,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
