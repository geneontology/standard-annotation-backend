"""Tests for the column type that stores string enums as `VARCHAR`."""

import pytest
from sqlalchemy.dialects import postgresql

from standard_annotation_backend.domain.jobs import JobStatus
from standard_annotation_backend.persistence.models import StrEnumColumn

_DIALECT = postgresql.dialect()


def test_written_values_are_stored_as_enum_strings() -> None:
    """Enum members and their string values are stored as the plain value."""
    column_type = StrEnumColumn(JobStatus, 50)

    assert column_type.process_bind_param(JobStatus.RUNNING, _DIALECT) == "running"
    assert column_type.process_bind_param("running", _DIALECT) == "running"
    assert column_type.process_bind_param(None, _DIALECT) is None


def test_stored_values_load_as_enum_members() -> None:
    """Reading a row returns the enum member, not a bare string."""
    column_type = StrEnumColumn(JobStatus, 50)

    loaded = column_type.process_result_value("failed", _DIALECT)

    assert loaded is JobStatus.FAILED
    assert column_type.process_result_value(None, _DIALECT) is None


def test_writing_an_unknown_value_is_rejected_before_the_database() -> None:
    """A value outside the enum cannot be written."""
    with pytest.raises(ValueError):
        StrEnumColumn(JobStatus, 50).process_bind_param("paused", _DIALECT)
