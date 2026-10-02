"""Define the interface that every kind of reference data refresh implements.

`RefreshRunner` drives one job through the same steps for every kind. A kind
supplies only what differs: where its sources are configured, how it recovers
work a previous attempt committed, how it recognizes an already-applied
document, how it applies a new one, and how it records a failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
)
from standard_annotation_backend.refresh.fetchers import SourceDocument
from standard_annotation_backend.refresh.sources import GitHubSource, HttpsSource
from standard_annotation_backend.services.job_service import Job


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Describe a successful refresh in the form stored on its job.

    Attributes:
        result: JSON-compatible result stored on the job.
        progress: Kind-specific counts. The runner adds `phase: completed`.
        warnings: Short messages stored on the job.
    """

    result: dict[str, object]
    progress: dict[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class TerminalRefreshError(Exception):
    """Report a failure that retrying cannot fix, with details for operators.

    Attributes:
        failure_code: Code recorded on the job and its failure audit event.
        failure_details: Optional JSON-compatible description, such as invalid
            rows. It must not contain URLs.
    """

    def __init__(
        self,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None = None,
    ) -> None:
        self.failure_code = failure_code
        self.failure_details = failure_details
        super().__init__(f"Refresh failed: {failure_code.value}")


class ProgressReporter(Protocol):
    """Replace a running job's progress, as a kind's `apply` advances.

    A kind may report its own phases, such as `staging` and `publishing`, while
    `apply` runs. The runner always sets `fetching` and `applying` before `apply`
    and `completed` or `failed` after it.
    """

    def __call__(
        self, progress: dict[str, object], warnings: tuple[str, ...] = ()
    ) -> None:
        """Store `progress` and `warnings` on the job."""
        ...


class RefreshKind(Protocol):
    """Supply the kind-specific steps of a refresh.

    `RefreshRunner.run` calls these methods in a fixed order for every job.
    """

    @property
    def name(self) -> RefreshKindName:
        """Return the kind's name."""
        ...

    @property
    def job_type(self) -> JobType:
        """Return the job type of this kind's refresh jobs."""
        ...

    @property
    def terminal_errors(self) -> Mapping[type[Exception], RefreshFailureCode]:
        """Map this kind's non-retryable exceptions to failure codes."""
        ...

    def sources(self) -> Mapping[str, GitHubSource | HttpsSource]:
        """Return this kind's configured sources by key."""
        ...

    def recover(self, job: Job) -> RefreshResult | None:
        """Return a result committed by an earlier attempt of this job, if any."""
        ...

    def unchanged(
        self, source_key: str, document: SourceDocument
    ) -> RefreshResult | None:
        """Return a result without applying when `document` is already active."""
        ...

    def apply(
        self, job: Job, document: SourceDocument, progress: ProgressReporter
    ) -> RefreshResult:
        """Apply a new document and return the committed result."""
        ...

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed, audit it, and clean up, in one transaction."""
        ...

    def after_terminal(self, source_key: str) -> None:
        """Do follow-up work once a job has succeeded or failed."""
        ...


class RefreshRetirer(Protocol):
    """Retire data for a source that is no longer configured."""

    def retire(self, job: Job, source_key: str) -> RefreshResult:
        """Retire the source's active data and return the committed result."""
        ...

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed, audit it, and clean up, in one transaction."""
        ...
