"""Structured logs: one JSON object per line on stderr, safe to keep.

A user is only ever shown a short message (an exception type at most). The cause is
needed to fix the problem, so it goes here instead: the chain of exceptions, where each
was raised, and the review and request it belongs to.

Three rules keep secrets and user content out of the log:

- Nothing is logged that a caller did not pass in. Terraform, prompts and replies are
  never passed in.
- Exception messages are redacted and cut short. Pydantic's messages quote the bad
  input, so for those only the field path and the error type are kept.
- Only this project's logger is configured. `httpx` logs every request URL at INFO.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from pydantic import ValidationError

LOGGER_NAME = "gcp_review"
MAX_MESSAGE_CHARS = 300
MAX_CHAIN = 5
MAX_FRAMES = 8
MIN_SECRET_CHARS = 8  # shorter values would redact ordinary words

logger = logging.getLogger(LOGGER_NAME)
logger.addHandler(logging.NullHandler())  # silent until a server calls configure_logging

_review_id: ContextVar[str | None] = ContextVar("review_id", default=None)

_SECRET_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|lsv2|AIza|ya29|ghp|gho)[-_.A-Za-z0-9*]{6,}"),
    re.compile(r"\bBearer\s+\S+", re.I),
    re.compile(
        r"\b(password|passwd|secret|token|api[_-]?key|key|credentials?)\b\W{0,3}"
        r"(?:\bis\b|=|:)\W{0,3}\S+",
        re.I,
    ),
    re.compile(r"\b(key|token|password|secret)=[^&\s]+", re.I),
)
REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Remove anything that is, or looks like, a credential, and cut the text short."""
    for name, value in os.environ.items():
        if _SECRET_ENV_NAME.search(name) and len(value) >= MIN_SECRET_CHARS:
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS] + "…"


@contextmanager
def review_context(review_id: str) -> Iterator[None]:
    """Everything logged inside the block carries this review id."""
    token = _review_id.set(review_id)
    try:
        yield
    finally:
        _review_id.reset(token)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Log that something happened. `event` is a fixed name, never user text."""
    if review_id := _review_id.get():
        fields.setdefault("review_id", review_id)
    fields = {name: value for name, value in fields.items() if value is not None}
    logger.log(level, event, extra={"fields": fields})


def log_failure(
    event: str, exc: BaseException, *, level: int = logging.ERROR, **fields: Any
) -> None:
    """Log a failure with its cause: the exceptions behind it and where they came from."""
    log_event(event, level=level, causes=describe_causes(exc), **fields)


def describe_causes(exc: BaseException) -> list[dict[str, Any]]:
    """The exception and what caused it, newest first."""
    causes: list[dict[str, Any]] = []
    current: BaseException | None = exc
    while current is not None and len(causes) < MAX_CHAIN:
        frames = traceback.extract_tb(current.__traceback__)[-MAX_FRAMES:]
        causes.append(
            {
                "type": type(current).__name__,
                "message": redact(_message(current)),
                "at": [f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}" for f in frames],
            }
        )
        hidden = current.__suppress_context__
        current = current.__cause__ or (None if hidden else current.__context__)
    return causes


def _message(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):  # its text quotes the input
        problems = exc.errors(include_input=False, include_url=False, include_context=False)
        return "; ".join(
            f"{'.'.join(str(part) for part in p['loc']) or '(root)'}: {p['type']}" for p in problems
        )
    return str(exc)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(line, default=str)


def configure_logging(stream: TextIO | None = None) -> None:
    """Called once by a server's entry point. LOG_LEVEL in the environment sets the level.

    Always stderr: the MCP server's stdout carries the protocol.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(os.getenv("LOG_LEVEL", "").strip().upper() or "INFO")
    logger.propagate = False
