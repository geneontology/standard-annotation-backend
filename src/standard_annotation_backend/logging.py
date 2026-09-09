"""Configure structured logging shared by all SAB processes.

Application code may emit native structlog events, while Uvicorn, Celery, and
third-party libraries generally emit standard-library `logging.LogRecord`
objects. This module routes both forms through one
`structlog.stdlib.ProcessorFormatter` pipeline so they receive the same core
fields and the renderer selected by `SAB_LOG_FORMAT`. The process-wide
handler writes to stdout for collection by the container runtime.

Uvicorn constructs `create_formatter()` through the `()` factory in
`uvicorn_logging.json` before importing the FastAPI application. Celery's
`setup_logging` signal calls `configure_logging()`, and other SAB process
entry points can do the same. Loading only `LoggingSettings` from the
formatter factory avoids requiring database and service settings during early
server logging initialization.

References:
    * Structlog's two-path `ProcessorFormatter` integration:
      https://www.structlog.org/en/26.1.0/standard-library.html#rendering-using-structlog-based-formatters-within-logging
    * Uvicorn's supported logging configuration interface:
      https://uvicorn.dev/concepts/logging/
"""

import logging
import sys
from collections.abc import Mapping
from typing import TextIO

import structlog
from structlog.types import EventDict, Processor

from standard_annotation_backend.config import LogFormat, get_logging_settings


def _format_foreign_mapping_args(
    _logger: object,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Apply standard-library mapping interpolation to a foreign record.

    Python logging accepts any `Mapping` for `msg % args`, whereas
    structlog's positional formatter is intended primarily for positional
    tuples. `ProcessorFormatter(pass_foreign_args=True)` exposes the
    original `LogRecord.args` as `positional_args` so this processor can
    format mappings first and prevent a second interpolation attempt.

    Args:
        _logger: Logger supplied by the structlog processor protocol; unused.
        _method_name: Log method supplied by the processor protocol; unused.
        event_dict: Event being normalized. The mapping is modified in place.

    Returns:
        The event with mapping arguments formatted, or the original event when
        it does not contain foreign mapping arguments.

    References:
        * Python's `LogRecord` interpolation behavior:
          https://docs.python.org/3/library/logging.html#logging.LogRecord
    """
    record = event_dict.get("_record")
    positional_args = event_dict.get("positional_args")
    if isinstance(record, logging.LogRecord) and isinstance(positional_args, Mapping):
        event_dict["event"] %= positional_args
        event_dict.pop("positional_args")
    return event_dict


def _drop_color_message(
    _logger: object,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Remove Uvicorn's redundant colorized message after extras are copied.

    Uvicorn attaches `color_message` to some records for its console
    formatter. SAB renders the ordinary event itself, so retaining that extra
    would duplicate the message and may preserve terminal escape codes in JSON.
    The logger-name check keeps an unrelated application's `color_message`
    field intact.

    Args:
        _logger: Logger supplied by the structlog processor protocol; unused.
        _method_name: Log method supplied by the processor protocol; unused.
        event_dict: Event being normalized. The mapping is modified in place.

    Returns:
        The event without Uvicorn's `color_message`, or the original event for
        any other logger.
    """
    record = event_dict.get("_record")
    if isinstance(record, logging.LogRecord) and (
        record.name == "uvicorn" or record.name.startswith("uvicorn.")
    ):
        event_dict.pop("color_message", None)
    return event_dict


def _split_client(client: str) -> dict[str, str | int] | None:
    """Split Uvicorn's `address:port` text.

    Splitting at the rightmost colon preserves an IPv6 address. `None`
    signals that access-log enrichment should fall back to the original record
    rather than fail while formatting a log.

    Args:
        client: Client endpoint rendered by Uvicorn as `address:port`.

    Returns:
        Separate `address` and numeric `port` fields, or `None` when the value
        cannot be parsed safely.
    """
    address, separator, raw_port = client.rpartition(":")
    if not separator or not address:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        return None
    return {"address": address, "port": port}


def add_uvicorn_access_fields(
    _logger: object,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Convert Uvicorn's current access arguments into queryable fields.

    Uvicorn currently logs `(client, method, target, version, status_code)` as
    five positional arguments. That tuple is an implementation detail rather
    than a documented extension interface, so every assumption is validated.
    An unfamiliar record is returned unchanged and remains renderable as an
    ordinary access-log message.

    Args:
        _logger: Logger supplied by the structlog processor protocol; unused.
        _method_name: Log method supplied by the processor protocol; unused.
        event_dict: Event being normalized. The mapping is modified in place
            when it contains the recognized Uvicorn access-record shape.

    Returns:
        A structured `http_request` event when the record matches Uvicorn's
        current shape, otherwise the unchanged event.

    References:
        * Uvicorn's current access-log emission:
          https://github.com/Kludex/uvicorn/blob/0.52.4/uvicorn/protocols/http/h11_impl.py
        * Uvicorn's supported access-log configuration:
          https://uvicorn.dev/concepts/logging/
    """
    record = event_dict.get("_record")
    if not isinstance(record, logging.LogRecord) or record.name != "uvicorn.access":
        return event_dict
    record_args = record.args or event_dict.get("positional_args")
    if not isinstance(record_args, tuple) or len(record_args) != 5:
        return event_dict

    client, method, target, version, status_code = record_args
    if not (
        isinstance(client, str)
        and isinstance(method, str)
        and isinstance(target, str)
        and isinstance(version, str)
        and isinstance(status_code, int)
    ):
        return event_dict

    client_fields = _split_client(client)
    if client_fields is None:
        return event_dict

    event_dict["event"] = "http_request"
    event_dict.pop("positional_args", None)
    event_dict["http"] = {
        "method": method,
        "target": target,
        "version": version,
        "status_code": status_code,
    }
    event_dict["client"] = client_fields
    return event_dict


def _shared_processors() -> list[Processor]:
    """Return the ordered normalization chain shared by both logging APIs.

    Native structlog events run this chain before `wrap_for_formatter`;
    foreign `LogRecord` objects run it as `ProcessorFormatter`'s
    `foreign_pre_chain`. Keeping the lists identical gives both sources the
    same schema. Order matters: raw arguments are inspected before formatting,
    while Uvicorn extras are removed only after `ExtraAdder` copies them.

    Returns:
        A new list containing the shared processors in execution order.
    """
    return [
        # Merge context-local values bound for the current request or task.
        # https://www.structlog.org/en/26.1.0/contextvars.html
        structlog.contextvars.merge_contextvars,
        # Add the originating logger name under the `logger` key.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.stdlib.add_logger_name
        structlog.stdlib.add_logger_name,
        # Add the normalized severity name under the `level` key.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.stdlib.add_log_level
        structlog.stdlib.add_log_level,
        # Preserve Python logging's mapping-based percent interpolation.
        _format_foreign_mapping_args,
        # Replace a recognized Uvicorn access record with structured HTTP data.
        add_uvicorn_access_fields,
        # Apply standard percent formatting to remaining positional arguments.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.stdlib.PositionalArgumentsFormatter
        structlog.stdlib.PositionalArgumentsFormatter(),
        # Copy nonstandard LogRecord attributes supplied through `extra`.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.stdlib.ExtraAdder
        structlog.stdlib.ExtraAdder(),
        # Discard Uvicorn's redundant `color_message` extra.
        _drop_color_message,
        # Add an ISO-8601 UTC timestamp to every event.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.processors.TimeStamper
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Render requested stack information under the `stack` key.
        # https://www.structlog.org/en/26.1.0/api.html#structlog.processors.StackInfoRenderer
        structlog.processors.StackInfoRenderer(),
    ]


def create_formatter(
    log_format: LogFormat | str | None = None,
) -> structlog.stdlib.ProcessorFormatter:
    """Build the common formatter for native and standard-library records.

    With no argument, the Uvicorn `dictConfig` factory path reads the format
    from `LoggingSettings`. Process entry points may instead pass an explicit
    `LogFormat`. Native structlog events traverse the shared chain
    before `wrap_for_formatter`; foreign records traverse the same chain as
    `foreign_pre_chain`. Internal processor metadata is removed before the
    selected console or JSON renderer produces the final string.

    `pass_foreign_args` and `use_get_message=False` preserve raw logging
    arguments until SAB's compatibility and Uvicorn processors inspect them.

    Args:
        log_format: Renderer to use. When omitted, load `SAB_LOG_FORMAT` through
            `LoggingSettings` for Uvicorn's formatter-factory path.

    Returns:
        A configured processor formatter that handles both native structlog
        events and standard-library records.

    References:
        * Structlog's `ProcessorFormatter` API:
          https://www.structlog.org/en/26.1.0/api.html#structlog.stdlib.ProcessorFormatter
    """
    selected_format = (
        get_logging_settings().log_format
        if log_format is None
        else LogFormat(log_format)
    )
    shared_processors = _shared_processors()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor
    final_processors: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta
    ]
    if selected_format is LogFormat.JSON:
        final_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    final_processors.append(renderer)

    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=final_processors,
        pass_foreign_args=True,
        use_get_message=False,
    )


def configure_logging(
    log_format: LogFormat | str,
    log_level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
) -> None:
    """Install one SAB-formatted stdout handler for the current process.

    This is the direct setup path used by Celery's `setup_logging` signal and
    any SAB-owned process bootstrap. Existing root handlers are replaced to
    avoid duplicate output, and Uvicorn loggers are made to propagate through
    the root handler instead of retaining server-specific handlers. Repeated
    calls therefore leave one active SAB handler. `stream` is available for
    callers that need an explicit destination; production callers use stdout.

    Args:
        log_format: Renderer to install for the process.
        log_level: Numeric or named standard-library logging level.
        stream: Optional output stream. Defaults to stdout.

    References:
        * Python's handler API:
          https://docs.python.org/3/library/logging.html#logging.Handler
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(create_formatter(log_format))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper() if isinstance(log_level, str) else log_level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
