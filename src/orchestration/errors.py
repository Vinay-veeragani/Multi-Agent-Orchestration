"""Error taxonomy for the orchestration engine.

The retry subsystem does not guess whether a failure is worth retrying: every
exception raised inside the engine carries that answer on itself via
:attr:`OrchestrationError.retryable`. This keeps the retry decision a property of
the *error*, declared where the error is raised, rather than a string-matching
heuristic living inside the retry loop.

Two roots:

``RetryableError``
    Transient. The same call, repeated later, may succeed -- timeouts, rate
    limits, upstream 5xx, lost connections.

``TerminalError``
    Deterministic. Repeating the call cannot change the outcome -- invalid input,
    permission denied, schema violation, policy violation, budget exhausted.

Anything that is *not* an :class:`OrchestrationError` (a stray ``KeyError``, say)
is treated as terminal by :func:`is_retryable`, because an unrecognised failure
is not evidence that retrying is safe.
"""

from __future__ import annotations

from typing import Any


class OrchestrationError(Exception):
    """Base class for every error raised by the engine.

    Args:
        message: Human-readable description. Must never contain secrets.
        context: Structured detail attached to events/spans for debugging.
    """

    #: Whether the retry engine may re-attempt the failed operation.
    retryable: bool = False
    #: Stable machine-readable code, surfaced in API responses and events.
    code: str = "orchestration_error"

    def __init__(self, message: str, /, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for persistence in events and execution state."""
        return {
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


class RetryableError(OrchestrationError):
    """Transient failure; retrying with backoff is appropriate."""

    retryable = True
    code = "retryable_error"


class TerminalError(OrchestrationError):
    """Deterministic failure; retrying cannot help."""

    retryable = False
    code = "terminal_error"


# ---------------------------------------------------------------------------
# Retryable: transient infrastructure and provider conditions
# ---------------------------------------------------------------------------


class EngineTimeoutError(RetryableError):
    """An operation exceeded its configured timeout."""

    code = "timeout"


class RateLimitError(RetryableError):
    """Provider rate limit (HTTP 429). May carry ``retry_after`` seconds."""

    code = "rate_limit"

    def __init__(self, message: str, /, retry_after: float | None = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.retry_after = retry_after


class ProviderUnavailableError(RetryableError):
    """Upstream LLM provider returned 5xx or was unreachable."""

    code = "provider_unavailable"


class NetworkError(RetryableError):
    """Connection reset, DNS failure, or other transport-level fault."""

    code = "network_error"


class StorageTransientError(RetryableError):
    """PostgreSQL/Redis connection interruption, deadlock, or serialisation failure."""

    code = "storage_transient"


class ConcurrencyConflictError(RetryableError):
    """Optimistic-concurrency or lock-acquisition conflict; safe to re-attempt."""

    code = "concurrency_conflict"


# ---------------------------------------------------------------------------
# Terminal: deterministic faults
# ---------------------------------------------------------------------------


class InputValidationError(TerminalError):
    """Input failed schema or semantic validation."""

    code = "validation_error"


class SchemaViolationError(TerminalError):
    """An LLM produced output that does not satisfy the required schema.

    Terminal by classification. The *repair* attempt performed by the structured
    output layer is a distinct mechanism from retry: it sends a different prompt
    (the validation errors), so it is not a blind re-attempt of the same call.
    """

    code = "schema_violation"


class PermissionDeniedError(TerminalError):
    """An agent attempted a tool it is not permitted to use."""

    code = "permission_denied"


class PolicyViolationError(TerminalError):
    """A policy rule rejected the requested action outright."""

    code = "policy_violation"


class ApprovalRejectedError(TerminalError):
    """A human reviewer rejected the requested action."""

    code = "approval_rejected"


class BudgetExceededError(TerminalError):
    """A hard budget limit was reached; execution must stop.

    Args:
        dimension: Which limit tripped, e.g. ``"cost_usd"`` or ``"agent_steps"``.
        limit: The configured ceiling.
        used: Consumption at the moment the check failed.
    """

    code = "budget_exceeded"

    def __init__(
        self,
        message: str,
        /,
        dimension: str,
        limit: float,
        used: float,
        **context: Any,
    ) -> None:
        super().__init__(message, dimension=dimension, limit=limit, used=used, **context)
        self.dimension = dimension
        self.limit = limit
        self.used = used


class NotFoundError(TerminalError):
    """A referenced agent, tool, workflow, or execution does not exist."""

    code = "not_found"


class DuplicateError(TerminalError):
    """Registration collided with an existing identifier."""

    code = "duplicate"


class GraphValidationError(TerminalError):
    """A workflow graph is structurally invalid (cycle, unknown node, unreachable)."""

    code = "graph_validation_error"

    def __init__(self, message: str, /, problems: list[str] | None = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.problems = problems or []

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["problems"] = self.problems
        return data


class InvalidStateTransitionError(TerminalError):
    """An execution was asked to move between statuses in a way the model forbids."""

    code = "invalid_state_transition"


class ExecutionCancelledError(TerminalError):
    """Execution was cancelled by an operator."""

    code = "cancelled"


class ConfigurationError(TerminalError):
    """The engine is misconfigured (missing credential, unusable provider)."""

    code = "configuration_error"


# ---------------------------------------------------------------------------
# Control-flow signals (not failures)
# ---------------------------------------------------------------------------


class ApprovalRequired(OrchestrationError):  # noqa: N818 - a signal, not an error
    """Raised to suspend execution pending human approval.

    This is control flow, not a fault: the engine catches it, checkpoints, marks
    the execution ``WAITING_FOR_APPROVAL`` and returns. It is deliberately not a
    subclass of :class:`TerminalError` so that no retry or failure handler treats
    a pause as a failure.
    """

    retryable = False
    code = "approval_required"

    def __init__(self, message: str, /, approval_id: str, **context: Any) -> None:
        super().__init__(message, approval_id=approval_id, **context)
        self.approval_id = approval_id


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

#: Exceptions raised by third-party libraries that we treat as transient.
_EXTERNAL_RETRYABLE: frozenset[str] = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "TimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "OperationalError",
        "InterfaceError",
        "DeadlockDetected",
        "SerializationFailure",
        "BusyConnectionError",
        "TooManyConnectionsError",
        "BusyLoadingError",
    }
)


def is_retryable(exc: BaseException) -> bool:
    """Return whether ``exc`` should be retried.

    Engine errors answer for themselves. Third-party exceptions are matched by
    class name against a conservative allowlist -- matching by name avoids
    importing httpx/asyncpg/redis just to build the taxonomy, and keeps the
    dependency direction pointing inward.

    Anything unrecognised is treated as **not** retryable: an unknown failure is
    not evidence that repeating a side effect is safe.
    """
    if isinstance(exc, ApprovalRequired):
        return False
    if isinstance(exc, OrchestrationError):
        return exc.retryable
    return any(klass.__name__ in _EXTERNAL_RETRYABLE for klass in type(exc).__mro__)


def error_code(exc: BaseException) -> str:
    """Stable machine-readable code for any exception."""
    if isinstance(exc, OrchestrationError):
        return exc.code
    return f"external:{type(exc).__name__}"


def to_error_dict(exc: BaseException) -> dict[str, Any]:
    """Serialise any exception for persistence, without leaking a traceback."""
    if isinstance(exc, OrchestrationError):
        return exc.to_dict()
    return {
        "code": error_code(exc),
        "type": type(exc).__name__,
        "message": str(exc),
        "retryable": is_retryable(exc),
        "context": {},
    }
