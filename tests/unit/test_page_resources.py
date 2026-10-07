"""Verify building paginated API responses from service result pages."""

from pydantic import BaseModel

from standard_annotation_backend.api.models import PageResource
from standard_annotation_backend.services.pagination import ResultPage


class _Item(BaseModel):
    label: str


class _ItemPage(PageResource[_Item]):
    """Page of test items."""


def test_from_result_page_converts_items_and_keeps_pagination() -> None:
    """Each result is converted in order, and pagination values are copied."""
    result = ResultPage(items=(1, 2), total=7, limit=2, offset=4)

    page = _ItemPage.from_result_page(result, lambda value: _Item(label=str(value)))

    assert page.model_dump() == {
        "items": [{"label": "1"}, {"label": "2"}],
        "total": 7,
        "limit": 2,
        "offset": 4,
    }
