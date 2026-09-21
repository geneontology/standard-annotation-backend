"""Define pagination results shared by application services."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResultPage[T]:
    """Hold one requested portion of a larger result set.

    Attributes:
        items: Results included in this page.
        total: Number of results across all pages.
        limit: Maximum number of results requested for this page.
        offset: Number of matching results skipped before this page.
    """

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int
