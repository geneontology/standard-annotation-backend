"""Define pagination results shared by application services."""

from collections.abc import Callable
from dataclasses import dataclass

from standard_annotation_backend.persistence.repositories import Page


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

    @staticmethod
    def from_page[S, R](
        page: Page[S],
        convert: Callable[[S], R],
        *,
        limit: int,
        offset: int,
    ) -> "ResultPage[R]":
        """Build a result page from a repository page.

        Args:
            page: Records and total loaded by a repository.
            convert: Function turning one record into an application result.
            limit: Maximum number of results requested for this page.
            offset: Number of matching results skipped before this page.

        Returns:
            The converted results, in record order, with the repository total and
            the requested window.
        """
        return ResultPage(
            items=tuple(convert(item) for item in page.items),
            total=page.total,
            limit=limit,
            offset=offset,
        )
