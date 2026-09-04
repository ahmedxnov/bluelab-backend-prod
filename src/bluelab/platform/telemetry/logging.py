"""Structured logging. Never logs a credential, a candidate token, a session id, or
any call content.

The rule is not a review convention here. `SENSITIVE_KEYS` is redacted on the way
out by a processor, so a log call that passes a token under a known key emits
`[redacted]` rather than the value — and `tools/check_telemetry_content_free.py`
scans for the rest (SEC-024, SEC-019).

Redaction is a backstop, not permission. The right move is still not to pass the
value; this exists because "someone will, eventually, in a debugging session at
2am" is a true statement about every codebase.

Every event carries the correlation spine, so a log line, a span, and a metric
sample for the same unit of work join on `request_id` (observability/01 §3).
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

from bluelab.platform.config import Environment, Settings
from bluelab.platform.telemetry import correlation

SENSITIVE_KEYS: Final = frozenset(
    {
        "password",
        "password_hash",
        "secret",
        "api_key",
        "token",
        "candidate_token",
        "reset_token",
        "session_id",
        "authorization",
        "cookie",
        "set_cookie",
        "x_agent_signature",
        "hmac",
        # Call content. The transcript never belongs in a log at any level.
        "transcript",
        "text",
        "quote",
        "utterance",
        "persona",
        "answer_key",
        "scenario",
    }
)

REDACTED: Final = "[redacted]"


_MAX_REDACT_DEPTH: Final = 6
"""Bound the walk. A cyclic or pathologically nested structure in a log call
must not turn a log line into a hang."""


def _redact_value(value: Any, depth: int) -> Any:
    """Recursively redact sensitive keys inside nested structures.

    Top-level-only redaction was the original bug: `extra={"payload": {"text":
    "..."}}` passed transcript content straight through, defeating the
    content-free guarantee SEC-024 scans for (observability/01 §5).
    """
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in SENSITIVE_KEYS else _redact_value(inner, depth + 1)
            for key, inner in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item, depth + 1) for item in value)
    return value


def _redact(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the value of any known-sensitive key, at any depth."""
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key], 0)
    return event_dict


def _add_correlation(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the correlation spine to every event."""
    current = correlation.current()
    if current is not None:
        event_dict.update(current.span_attributes())
    return event_dict


def configure(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger.

    JSON everywhere except local development, where a console renderer is worth
    the divergence — this is formatting, not behaviour, so it does not violate
    the parity rules (infra/00 §2).
    """
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.environment is Environment.LOCAL
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        force=True,
    )
    # Quiet the libraries that log a line per statement or per request; the
    # signal we want from them is already in the spans.
    for noisy in ("sqlalchemy.engine", "uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A bound logger for `name`."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
