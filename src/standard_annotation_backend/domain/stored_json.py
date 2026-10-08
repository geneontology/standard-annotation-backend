"""Write JSON that SAB stores in the database and validate it when read back.

Job results, refresh results, rejection reports, and change-set previews are
stored as JSON and later rebuilt into typed values. `StoredJson` gives each such
shape one definition, used for both directions, so writing and reading cannot
drift apart and malformed data is reported instead of being trusted.
"""

import json

from pydantic import TypeAdapter


class StoredDataError(RuntimeError):
    """Report stored JSON that does not have its expected shape.

    The message names what was being read and never includes stored values, and
    the validation error is not chained, so the exception is safe to log. It is not
    a terminal refresh failure: a refresh job that meets it is retried.
    """

    def __init__(self, label: str) -> None:
        super().__init__(f"stored {label} is invalid")


class StoredJson[T]:
    """Convert one stored shape between its typed value and JSON-compatible data.

    Reading validates in Pydantic's strict JSON mode: UUIDs, datetimes, and enums
    are read from their string forms and arrays become tuples, but no other
    conversion happens, so `true` or `"1"` is not accepted as a count. Unknown keys
    are ignored, which lets writers add derived values beside the fields.

    Args:
        value_type: The stored type, usually a frozen dataclass.
        label: What the value is, used in `StoredDataError` messages.
    """

    def __init__(self, value_type: type[T], *, label: str) -> None:
        self._adapter: TypeAdapter[T] = TypeAdapter(value_type)
        self._label = label

    def dump(self, value: T) -> dict[str, object]:
        """Return the value as JSON-compatible data for a JSON column."""
        return self._adapter.dump_python(value, mode="json")

    def load(self, value: object) -> T:
        """Return the typed value for data read from a JSON column.

        Raises:
            StoredDataError: If the data does not have the stored shape.
        """
        try:
            return self._adapter.validate_json(json.dumps(value), strict=True)
        except (TypeError, ValueError):
            # `ValidationError` is a `ValueError`. Dropping the cause keeps stored
            # values out of logs.
            raise StoredDataError(self._label) from None
