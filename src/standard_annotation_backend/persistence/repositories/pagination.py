"""Load stable pages of SQLAlchemy records."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select, true
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Hold selected results and the size of the complete result set.

    Attributes:
        items: Records included in the requested page.
        total: Number of records matching the query before pagination.
    """

    items: tuple[T, ...]
    total: int


def _load_page[T](
    session: Session,
    statement: Select[tuple[T]],
    *,
    record_type: type[T],
    order_by: Sequence[InstrumentedAttribute[Any]],
    limit: int,
    offset: int,
) -> Page[T]:
    """Load a page and its total using one database snapshot.

    The statement must select one SQLAlchemy record type. The ordering fields
    must belong to that record type and together provide a stable result order.

    Args:
        session: Database session used to execute the query.
        statement: Query selecting the complete filtered result set.
        record_type: SQLAlchemy record returned by the query.
        order_by: Fields that determine the stable result order.
        limit: Maximum number of records to return.
        offset: Number of matching records to skip.

    Returns:
        The requested records and the total before pagination.
    """
    filtered_records = statement.cte()
    filtered_record = aliased(record_type, filtered_records)
    filtered_order = tuple(getattr(filtered_record, field.key) for field in order_by)
    page = (
        select(filtered_record)
        .order_by(*filtered_order)
        .limit(limit)
        .offset(offset)
        .cte()
    )
    page_record = aliased(record_type, page)
    page_order = tuple(getattr(page_record, field.key) for field in order_by)
    summary = select(func.count().label("total")).select_from(filtered_records).cte()
    rows = session.execute(
        select(page_record, summary.c.total)
        .select_from(summary.outerjoin(page, true()))
        .order_by(*page_order)
    ).all()
    total = rows[0].total
    assert total is not None
    items = tuple(row[0] for row in rows if row[0] is not None)
    return Page(items=items, total=total)
