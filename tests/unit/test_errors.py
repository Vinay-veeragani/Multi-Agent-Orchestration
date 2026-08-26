"""Tests for the error taxonomy.

The central property under test: retryability is decided by classification, and
anything unrecognised is treated as terminal. That default is a safety property --
retrying an unknown failure can repeat a side effect.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestration.errors import (
    ApprovalRejectedError,
    ApprovalRequired,
    BudgetExceededError,
    ConcurrencyConflictError,
    ConfigurationError,
    DuplicateError,
    EngineTimeoutError,
    ExecutionCancelledError,
    GraphValidationError,
    InputValidationError,
    InvalidStateTransitionError,
    NetworkError,
    NotFoundError,
    OrchestrationError,
    PermissionDeniedError,
    PolicyViolationError,
    ProviderUnavailableError,
    RateLimitError,
    RetryableError,
    SchemaViolationError,
    StorageTransientError,
    TerminalError,
    error_code,
    is_retryable,
    to_error_dict,
)

pytestmark = pytest.mark.unit


RETRYABLE_CLASSES: list[type[OrchestrationError]] = [
    EngineTimeoutError,
    RateLimitError,
    ProviderUnavailableError,
    NetworkError,
    StorageTransientError,
    ConcurrencyConflictError,
]

TERMINAL_CLASSES: list[type[OrchestrationError]] = [
    InputValidationError,
    SchemaViolationError,
    PermissionDeniedError,
    PolicyViolationError,
    ApprovalRejectedError,
    NotFoundError,
    DuplicateError,
    GraphValidationError,
    InvalidStateTransitionError,
    ExecutionCancelledError,
    ConfigurationError,
]


@pytest.mark.parametrize("klass", RETRYABLE_CLASSES)
def test_retryable_errors_are_retryable(klass: type[OrchestrationError]) -> None:
    exc = klass("boom")
    assert exc.retryable is True
    assert is_retryable(exc) is True
    assert isinstance(exc, RetryableError)


@pytest.mark.parametrize("klass", TERMINAL_CLASSES)
def test_terminal_errors_are_not_retryable(klass: type[OrchestrationError]) -> None:
    exc = klass("boom")
    assert exc.retryable is False
    assert is_retryable(exc) is False
    assert isinstance(exc, TerminalError)


def test_every_error_has_a_distinct_code() -> None:
    """Codes are an API surface; a collision would make them useless for dispatch."""
    classes = [*RETRYABLE_CLASSES, *TERMINAL_CLASSES]
    codes = [c.code for c in classes]
    assert len(codes) == len(set(codes)), f"duplicate error codes: {codes}"


class TestExternalExceptionClassification:
    """Third-party and builtin exceptions are classified by name allowlist."""

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError(),  # asyncio.TimeoutError is this same class on 3.11+
            ConnectionError(),
            ConnectionResetError(),
            ConnectionRefusedError(),
        ],
    )
    def test_transport_failures_are_retryable(self, exc: BaseException) -> None:
        assert is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("missing"),
            ValueError("bad"),
            TypeError("wrong"),
            AttributeError("nope"),
            ZeroDivisionError(),
            RuntimeError("unexpected"),
        ],
    )
    def test_unknown_failures_default_to_terminal(self, exc: BaseException) -> None:
        """An unrecognised failure is not evidence that a retry is safe."""
        assert is_retryable(exc) is False

    def test_cancellation_is_never_retried(self) -> None:
        """Retrying a cancelled operation would defeat cancellation entirely."""
        assert is_retryable(asyncio.CancelledError()) is False

    def test_subclass_of_allowlisted_name_is_retryable(self) -> None:
        class MyConnectionError(ConnectionError):
            pass

        assert is_retryable(MyConnectionError()) is True


class TestApprovalRequired:
    """ApprovalRequired is control flow, not a failure."""

    def test_is_not_a_terminal_error(self) -> None:
        """It must not be caught by failure handlers as a fault."""
        exc = ApprovalRequired("needs sign-off", approval_id="appr_1")
        assert not isinstance(exc, TerminalError)
        assert not isinstance(exc, RetryableError)
        assert isinstance(exc, OrchestrationError)

    def test_is_not_retryable(self) -> None:
        assert is_retryable(ApprovalRequired("x", approval_id="a")) is False

    def test_carries_the_approval_id(self) -> None:
        exc = ApprovalRequired("x", approval_id="appr_42")
        assert exc.approval_id == "appr_42"
        assert exc.context["approval_id"] == "appr_42"


class TestErrorContext:
    def test_context_is_rendered_in_str(self) -> None:
        exc = PermissionDeniedError("denied", agent="research_agent", tool="send_email")
        rendered = str(exc)
        assert "denied" in rendered
        assert "agent='research_agent'" in rendered
        assert "tool='send_email'" in rendered

    def test_str_without_context_is_just_the_message(self) -> None:
        assert str(NotFoundError("gone")) == "gone"

    def test_to_dict_is_serialisable(self) -> None:
        exc = EngineTimeoutError("slow", provider="openai", seconds=60)
        data = exc.to_dict()
        assert data == {
            "code": "timeout",
            "type": "EngineTimeoutError",
            "message": "slow",
            "retryable": True,
            "context": {"provider": "openai", "seconds": 60},
        }


class TestBudgetExceededError:
    def test_records_which_dimension_tripped(self) -> None:
        exc = BudgetExceededError("over", dimension="cost_usd", limit=0.5, used=0.61)
        assert exc.dimension == "cost_usd"
        assert exc.limit == 0.5
        assert exc.used == 0.61

    def test_dimension_appears_in_serialised_context(self) -> None:
        """The API returns a clear budget-exceeded reason, so it must round-trip."""
        exc = BudgetExceededError("over", dimension="tokens", limit=100, used=140)
        assert exc.to_dict()["context"]["dimension"] == "tokens"

    def test_is_not_retryable(self) -> None:
        """Retrying an exhausted budget would only burn more of it."""
        exc = BudgetExceededError("over", dimension="tokens", limit=1, used=2)
        assert is_retryable(exc) is False


class TestGraphValidationError:
    def test_collects_multiple_problems(self) -> None:
        """Graph validation reports every defect, not just the first."""
        exc = GraphValidationError(
            "invalid graph",
            problems=["cycle: a -> b -> a", "unknown agent: ghost", "unreachable: z"],
        )
        assert len(exc.problems) == 3
        assert exc.to_dict()["problems"] == exc.problems

    def test_defaults_to_empty_problem_list(self) -> None:
        assert GraphValidationError("bad").problems == []


class TestErrorCodeHelpers:
    def test_engine_errors_use_their_declared_code(self) -> None:
        assert error_code(RateLimitError("429")) == "rate_limit"

    def test_external_errors_are_namespaced(self) -> None:
        """Namespacing keeps a library's exception name from colliding with ours."""
        assert error_code(KeyError("k")) == "external:KeyError"

    def test_to_error_dict_handles_external_exceptions(self) -> None:
        data = to_error_dict(ValueError("bad input"))
        assert data["code"] == "external:ValueError"
        assert data["retryable"] is False
        assert data["context"] == {}

    def test_to_error_dict_never_includes_a_traceback(self) -> None:
        """Tracebacks can carry local variables, which can carry secrets."""
        try:
            raise EngineTimeoutError("slow", token="sk-should-not-appear")
        except EngineTimeoutError as exc:
            data = to_error_dict(exc)
        assert "traceback" not in data
        assert set(data) == {"code", "type", "message", "retryable", "context"}


class TestRateLimitRetryAfter:
    def test_retry_after_is_optional(self) -> None:
        assert RateLimitError("429").retry_after is None

    def test_retry_after_is_preserved(self) -> None:
        assert RateLimitError("429", retry_after=12.5).retry_after == 12.5
