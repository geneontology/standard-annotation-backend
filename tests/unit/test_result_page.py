"""Verify building service result pages from repository pages."""

from standard_annotation_backend.persistence.repositories import Page
from standard_annotation_backend.services.pagination import ResultPage


def test_from_page_converts_items_and_keeps_request_and_total() -> None:
    """Each item is converted in order, with the total and requested window kept."""
    page = Page(items=(1, 2, 3), total=10)

    result = ResultPage.from_page(page, str, limit=3, offset=6)

    assert result == ResultPage(items=("1", "2", "3"), total=10, limit=3, offset=6)


def test_from_page_keeps_total_for_empty_page() -> None:
    """A page past the end has no items but still reports the full total."""
    page: Page[int] = Page(items=(), total=4)

    result = ResultPage.from_page(page, str, limit=50, offset=100)

    assert result == ResultPage(items=(), total=4, limit=50, offset=100)
