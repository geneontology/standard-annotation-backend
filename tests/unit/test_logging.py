"""Focused tests for SAB-owned logging behavior."""

import json
import logging
import secrets
import subprocess
import sys

import pytest

from standard_annotation_backend.config import LogFormat
from standard_annotation_backend.logging import (
    add_uvicorn_access_fields,
    create_formatter,
)


@pytest.mark.parametrize("log_format", [LogFormat.CONSOLE, LogFormat.JSON])
def test_exception_logs_do_not_display_credential_locals(log_format: LogFormat) -> None:
    """Error tracebacks retain failure context without displaying local credentials."""
    raw_credential = secrets.token_urlsafe(32)

    def fail_with_credential_in_scope(credential: str) -> None:
        raise RuntimeError("safe failure message")

    try:
        fail_with_credential_in_scope(raw_credential)
    except RuntimeError:
        record = logging.LogRecord(
            "sab.test",
            logging.ERROR,
            __file__,
            1,
            "operation_failed",
            (),
            sys.exc_info(),
        )
        rendered = create_formatter(log_format).format(record)
    assert raw_credential not in rendered
    assert "RuntimeError" in rendered
    assert "safe failure message" in rendered


@pytest.mark.parametrize("path", ["/auth/github/callback", "/auth/github/callback/"])
def test_oauth_callback_access_logs_exclude_sensitive_query_values(path: str) -> None:
    """Callback access logs preserve HTTP structure without OAuth codes or state."""
    for client in ("127.0.0.1:12345", "unknown"):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (
                client,
                "GET",
                path + "?code=secret-code&state=secret-state",
                "1.1",
                204,
            ),
            None,
        )
        output = create_formatter(LogFormat.JSON).format(record)
        assert "secret-code" not in output and "secret-state" not in output
        transformed = json.loads(output)
        if client != "unknown":
            assert transformed["http"] == {
                "method": "GET",
                "target": path,
                "version": "1.1",
                "status_code": 204,
            }
            assert transformed["event"] == "http_request"
        else:
            assert path + " HTTP/1.1" in transformed["event"]


def test_json_pipeline_formats_structured_and_standard_library_records() -> None:
    """Both supported logging APIs must produce the shared JSON schema."""
    script = """
import logging
from collections.abc import Mapping

import structlog

from standard_annotation_backend.config import LogFormat
from standard_annotation_backend.logging import configure_logging


class DefaultingMapping(Mapping[str, object]):
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values.get(key, f"fallback-{key}")

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


configure_logging(LogFormat.JSON)
structlog.stdlib.get_logger("sab.test").info(
    "annotation_created", annotation_id="example-annotation"
)
logging.getLogger("dependency.test").warning(
    "retry %d", 2, extra={"operation": "fetch"}
)
logging.getLogger("dependency.test").info(
    "mapped retry %(attempt)s",
    {"attempt": 3},
    extra={"color_message": "caller-owned-value"},
)
logging.getLogger("dependency.test").info(
    "mapped missing %(missing)s",
    DefaultingMapping({"present": "value"}),
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    records = [json.loads(line) for line in completed.stdout.splitlines()]

    assert len(records) == 4
    assert records[0]["event"] == "annotation_created"
    assert records[0]["annotation_id"] == "example-annotation"
    assert records[0]["logger"] == "sab.test"
    assert records[0]["level"] == "info"
    assert "timestamp" in records[0]
    assert records[1]["event"] == "retry 2"
    assert records[1]["operation"] == "fetch"
    assert records[1]["logger"] == "dependency.test"
    assert records[1]["level"] == "warning"
    assert records[2]["event"] == "mapped retry 3"
    assert records[2]["color_message"] == "caller-owned-value"
    assert records[2]["logger"] == "dependency.test"
    assert records[2]["level"] == "info"
    assert records[3]["event"] == "mapped missing fallback-missing"
    assert records[3]["logger"] == "dependency.test"
    assert records[3]["level"] == "info"
    assert completed.stderr == ""


def test_uvicorn_access_record_becomes_structured_http_event_or_falls_back() -> None:
    """SAB enriches the known Uvicorn contract without making logging fragile."""
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("2001:db8::1:54321", "GET", "/health?full=true", "1.1", 200),
        None,
    )
    transformed = json.loads(create_formatter(LogFormat.JSON).format(record))

    assert transformed["event"] == "http_request"
    assert transformed["http"] == {
        "method": "GET",
        "target": "/health?full=true",
        "version": "1.1",
        "status_code": 200,
    }
    assert transformed["client"] == {"address": "2001:db8::1", "port": 54321}

    changed_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "changed access contract: %s",
        ("unknown",),
        None,
    )
    fallback = {
        "event": changed_record.getMessage(),
        "_record": changed_record,
    }

    assert add_uvicorn_access_fields(None, "info", fallback)["event"] == (
        "changed access contract: unknown"
    )

    for unknown_client in ("unknown", "", "unknown:not-a-port"):
        unknown_client_record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (unknown_client, "GET", "/health", "1.1", 200),
            None,
        )

        unknown_client_fallback = json.loads(
            create_formatter(LogFormat.JSON).format(unknown_client_record)
        )

        assert unknown_client_fallback["event"] == (
            f'{unknown_client} - "GET /health HTTP/1.1" 200'
        )
        assert "http" not in unknown_client_fallback
        assert "client" not in unknown_client_fallback
