"""Structured logging, in two renderings of the same events.

Every log line is an *event name* plus key/value fields — ``fetch.retry
url=... attempt=2 of=3`` — never a prose sentence with values interpolated into
it. The reason is entirely practical: a client watching a long run wants to
skim, and whoever debugs it in six months wants to ``grep`` and count. Prose
does neither.

``log_format: json`` emits the same events as one JSON object per line, which is
what a log collector wants. The text renderer is the human one and is what the
README screenshots show.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_LOGGER_NAME = "catalog_scraper"


class _TextFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL event key=value key=value``."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", record.getMessage())
        fields: dict[str, Any] = getattr(record, "fields", {})
        rendered = " ".join(f"{key}={_scalar(value)}" for key, value in fields.items())
        stamp = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        line = f"{stamp} {record.levelname:<7} {event}"
        return f"{line} {rendered}" if rendered else line


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=str)


def _scalar(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


class RunLogger:
    """A thin façade over :mod:`logging` that only accepts structured events."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def _emit(self, level: int, event: str, fields: dict[str, Any]) -> None:
        self._logger.log(level, event, extra={"event": event, "fields": fields})


def configure_logging(
    *, level: str = "INFO", log_format: str = "text", stream: TextIO | None = None
) -> RunLogger:
    """Install the handler and return the logger the pipeline should use.

    Logs go to **stderr** so that ``... | jq`` on the exported JSON keeps
    working: mixing diagnostics into a data stream is a small cruelty that
    breaks every downstream pipe.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, level))
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_JsonFormatter() if log_format == "json" else _TextFormatter())
    logger.addHandler(handler)
    return RunLogger(logger)


def null_logger() -> RunLogger:
    """A logger that discards everything. For unit tests of non-logging behaviour."""
    logger = logging.getLogger(f"{_LOGGER_NAME}.null")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return RunLogger(logger)
