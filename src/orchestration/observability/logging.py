"""Structured logging.

One rule matters more than any formatting choice: **a secret must never reach a
log line.** Every event dict passes through :func:`redact_processor` before it
is rendered, so a stray ``api_key`` argument, an Authorization header captured
by a debugging print, or a DSN with an embedded password is masked regardless
of which code path produced it. This is a backstop, not the only control --
:mod:`orchestration.config` already redacts DSNs and :mod:`orchestration.policies`
already redacts tool arguments -- but a backstop that runs on every single log
record is what catches the case nobody thought to redact explicitly.

structlog is configured once, at process start, via :func:`configure_logging`.
Everything else calls :func:`get_logger` and binds context as it goes.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from orchestration.config import Settings, get_settings

#: Key name fragments that mark a value as sensitive, matched case-insensitively
#: against every key in every event dict. A fragment match (not equality) is
#: deliberate: "openai_api_key" and "db_password" must be caught alongside the
#: bare "api_key" and "password".
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "access_key",
    "session_id",
    "cookie",
    "dsn",
    "connection_string",
)

#: Matches an embedded URL credential, e.g. ``user:pass@host``, so a raw DSN or
#: connection string logged as a plain string (not a keyed field) is still
#: caught even though key-based redaction cannot see inside a string value.
_URL_CREDENTIAL_RE = re.compile(r"://([^:/@\s]+):([^@\s]+)@")

#: Matches something that looks like a bearer token or long API key embedded in
#: free text, e.g. an error message that happened to include one.
_BEARER_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,})\b")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_string(value: str) -> str:
    """Mask credentials embedded inside a string value, not just its key."""
    value = _URL_CREDENTIAL_RE.sub(r"://\1:***@", value)
    return _BEARER_RE.sub("***REDACTED***", value)


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:  # pragma: no cover - defensive against pathological nesting
        return value
    if isinstance(value, dict):
        return {
            k: ("***" if _is_sensitive_key(str(k)) else _redact_value(v, depth=depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        redacted = [_redact_value(v, depth=depth + 1) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_processor(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact sensitive keys and embedded credentials.

    Runs on every event dict, so it is the one place secret-handling has to be
    correct rather than a property every call site must remember to uphold.
    """
    return {
        key: ("***" if _is_sensitive_key(str(key)) else _redact_value(value))
        for key, value in event_dict.items()
    }


def _add_service_context(service_name: str) -> Processor:
    """Bind static fields (service name) onto every event without repetition."""

    def _processor(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service_name)
        return event_dict

    return _processor


_configured = False


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and the stdlib logging root once per process.

    Idempotent: calling it again is a no-op, which matters because tests and the
    CLI may both want logging configured without caring who got there first.
    """
    global _configured
    if _configured:
        return

    config = settings or get_settings()

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_service_context(config.service_name),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]

    if config.log_format == "json":
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, config.log_level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        stream=sys.stderr,
        format="%(message)s",
    )
    # Quiet the chatty libraries; their INFO logs are rarely useful and would
    # otherwise double up with the engine's own structured events.
    for noisy in ("httpx", "httpcore", "asyncio", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def reset_logging_config() -> None:
    """Undo :func:`configure_logging`. For tests that need a clean process state."""
    global _configured
    _configured = False
    structlog.reset_defaults()


def get_logger(**initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a logger, configuring logging on first use if nobody has yet.

    Auto-configuring here (rather than requiring every entry point to call
    :func:`configure_logging` first) means a unit test that imports a module
    calling ``get_logger()`` at import time does not need boilerplate setup.
    """
    if not _configured:
        configure_logging()
    return structlog.get_logger(**initial_context)  # type: ignore[no-any-return]


def bind_execution_context(*, execution_id: str, trace_id: str | None = None, **extra: Any) -> None:
    """Bind execution identifiers into contextvars for the current async task.

    Every subsequent log call on this task -- including from code that never
    received ``execution_id`` as an argument -- carries it automatically. Bound
    via contextvars rather than a logger instance because the engine's call
    graph passes through many independent functions per execution.
    """
    structlog.contextvars.bind_contextvars(execution_id=execution_id, trace_id=trace_id, **extra)


def clear_execution_context() -> None:
    structlog.contextvars.clear_contextvars()
