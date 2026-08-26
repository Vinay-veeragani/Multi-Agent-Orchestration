"""Tests for :class:`RetryPolicy`.

The policy is pure: no sleeping, no clock, no I/O. These tests therefore assert
exact arithmetic rather than timing behaviour, which is what makes them fast and
non-flaky.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from orchestration.domain.retry import (
    DEFAULT_RETRY_POLICY,
    NETWORK_RETRY_POLICY,
    NO_RETRY_POLICY,
    RATE_LIMIT_RETRY_POLICY,
    RetryPolicy,
)
from orchestration.errors import (
    EngineTimeoutError,
    PermissionDeniedError,
    PolicyViolationError,
    RateLimitError,
    SchemaViolationError,
    StorageTransientError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fixed_policy() -> RetryPolicy:
    """Jitter disabled so backoff is exactly predictable."""
    return RetryPolicy(
        max_attempts=4,
        initial_backoff_seconds=1.0,
        multiplier=2.0,
        max_backoff_seconds=10.0,
        jitter="none",
    )


class TestConstruction:
    def test_defaults_are_conservative(self) -> None:
        assert DEFAULT_RETRY_POLICY.max_attempts == 3
        assert DEFAULT_RETRY_POLICY.jitter == "full"

    def test_max_retries_excludes_the_first_attempt(self) -> None:
        assert RetryPolicy(max_attempts=3).max_retries == 2
        assert NO_RETRY_POLICY.max_retries == 0

    def test_rejects_max_backoff_below_initial(self) -> None:
        with pytest.raises(ValidationError, match="max_backoff_seconds must be >="):
            RetryPolicy(initial_backoff_seconds=10.0, max_backoff_seconds=1.0)

    def test_rejects_a_code_in_both_allow_and_deny_lists(self) -> None:
        """A contradictory policy is a configuration bug, caught at definition."""
        with pytest.raises(ValidationError, match="both retry_on and never_retry_on"):
            RetryPolicy(retry_on=frozenset({"timeout"}), never_retry_on=frozenset({"timeout"}))

    def test_rejects_out_of_range_attempts(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=0)


class TestShouldRetry:
    def test_retries_transient_failures_within_the_attempt_budget(
        self, fixed_policy: RetryPolicy
    ) -> None:
        assert fixed_policy.should_retry(1, EngineTimeoutError("t")) is True
        assert fixed_policy.should_retry(3, StorageTransientError("db")) is True

    def test_stops_at_the_attempt_ceiling(self, fixed_policy: RetryPolicy) -> None:
        assert fixed_policy.should_retry(4, EngineTimeoutError("t")) is False
        assert fixed_policy.should_retry(99, EngineTimeoutError("t")) is False

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionDeniedError("denied"),
            PolicyViolationError("policy"),
            SchemaViolationError("malformed"),
        ],
    )
    def test_never_retries_deterministic_failures(
        self, fixed_policy: RetryPolicy, exc: Exception
    ) -> None:
        """These cannot succeed on a second identical attempt."""
        assert fixed_policy.should_retry(1, exc) is False

    def test_no_retry_policy_refuses_everything(self) -> None:
        assert NO_RETRY_POLICY.should_retry(1, EngineTimeoutError("t")) is False

    def test_never_retry_on_overrides_a_retryable_error(self) -> None:
        """An operator can pin down a misbehaving error class without code changes."""
        policy = RetryPolicy(max_attempts=5, never_retry_on=frozenset({"timeout"}))
        assert policy.should_retry(1, EngineTimeoutError("t")) is False
        assert policy.should_retry(1, StorageTransientError("db")) is True

    def test_retry_on_allowlist_excludes_unlisted_codes(self) -> None:
        policy = RetryPolicy(max_attempts=5, retry_on=frozenset({"rate_limit"}))
        assert policy.should_retry(1, RateLimitError("429")) is True
        assert policy.should_retry(1, EngineTimeoutError("t")) is False

    def test_unknown_exceptions_are_not_retried(self, fixed_policy: RetryPolicy) -> None:
        assert fixed_policy.should_retry(1, KeyError("mystery")) is False


class TestBackoff:
    def test_grows_exponentially(self, fixed_policy: RetryPolicy) -> None:
        assert [fixed_policy.backoff_for(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]

    def test_is_clamped_to_max(self, fixed_policy: RetryPolicy) -> None:
        assert fixed_policy.backoff_for(10) == 10.0

    def test_zero_initial_backoff_stays_zero(self) -> None:
        policy = RetryPolicy(initial_backoff_seconds=0.0, max_backoff_seconds=0.0)
        assert policy.backoff_for(1) == 0.0
        assert policy.backoff_for(5) == 0.0

    def test_full_jitter_stays_within_the_window(self, rng: random.Random) -> None:
        policy = RetryPolicy(
            max_attempts=5, initial_backoff_seconds=2.0, multiplier=2.0, jitter="full"
        )
        for attempt in range(1, 5):
            ceiling = min(2.0 * 2 ** (attempt - 1), policy.max_backoff_seconds)
            for _ in range(50):
                delay = policy.backoff_for(attempt, rng=rng)
                assert 0.0 <= delay <= ceiling

    def test_equal_jitter_keeps_a_guaranteed_floor(self, rng: random.Random) -> None:
        """Equal jitter bounds the worst case *and* guarantees a minimum wait."""
        policy = RetryPolicy(initial_backoff_seconds=4.0, jitter="equal")
        for _ in range(50):
            delay = policy.backoff_for(1, rng=rng)
            assert 2.0 <= delay <= 4.0

    def test_none_jitter_is_exact(self) -> None:
        policy = RetryPolicy(initial_backoff_seconds=3.0, jitter="none")
        assert policy.backoff_for(1) == 3.0


class TestRetryAfterHint:
    def test_provider_hint_wins_when_longer(self) -> None:
        """A provider telling us when to return beats our own guess."""
        policy = RetryPolicy(initial_backoff_seconds=0.5, max_backoff_seconds=60.0, jitter="none")
        assert policy.backoff_for(1, RateLimitError("429", retry_after=7.5)) == 7.5

    def test_computed_backoff_wins_when_longer(self) -> None:
        policy = RetryPolicy(initial_backoff_seconds=10.0, max_backoff_seconds=60.0, jitter="none")
        assert policy.backoff_for(1, RateLimitError("429", retry_after=1.0)) == 10.0

    def test_hostile_hint_is_clamped_to_max_backoff(self) -> None:
        """A buggy or hostile header must not stall an execution indefinitely."""
        policy = RetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=30.0, jitter="none")
        assert policy.backoff_for(1, RateLimitError("429", retry_after=999_999)) == 30.0

    def test_hint_ignored_when_disabled(self) -> None:
        policy = RetryPolicy(initial_backoff_seconds=1.0, jitter="none", respect_retry_after=False)
        assert policy.backoff_for(1, RateLimitError("429", retry_after=50.0)) == 1.0

    def test_hint_on_a_non_rate_limit_error_is_ignored(self) -> None:
        policy = RetryPolicy(initial_backoff_seconds=1.0, jitter="none")
        assert policy.backoff_for(1, EngineTimeoutError("t")) == 1.0


class TestDeterministicBackoff:
    def test_same_seed_gives_the_same_delay(self, fixed_policy: RetryPolicy) -> None:
        """The benchmark depends on this: latency must be reproducible."""
        policy = fixed_policy.model_copy(update={"jitter": "full"})
        first = policy.deterministic_backoff_for(2, "scenario-a")
        second = policy.deterministic_backoff_for(2, "scenario-a")
        assert first == second

    def test_different_seeds_give_different_delays(self, fixed_policy: RetryPolicy) -> None:
        policy = fixed_policy.model_copy(update={"jitter": "full"})
        delays = {policy.deterministic_backoff_for(2, f"seed-{i}") for i in range(20)}
        assert len(delays) > 1, "seeded backoff is not varying with the seed"

    def test_stays_within_the_jitter_window(self, fixed_policy: RetryPolicy) -> None:
        policy = fixed_policy.model_copy(update={"jitter": "full"})
        for i in range(1, 5):
            ceiling = min(1.0 * 2 ** (i - 1), 10.0)
            assert 0.0 <= policy.deterministic_backoff_for(i, "s") <= ceiling


class TestWorstCaseDelay:
    def test_sums_the_retry_waits(self, fixed_policy: RetryPolicy) -> None:
        # 3 retries after the first attempt: 1 + 2 + 4
        assert fixed_policy.total_worst_case_delay() == 7.0

    def test_respects_the_clamp(self) -> None:
        policy = RetryPolicy(
            max_attempts=5, initial_backoff_seconds=10.0, multiplier=10.0, max_backoff_seconds=15.0
        )
        # 4 retries; waits are 10, then 100/1000/10000 all clamped to 15
        assert policy.total_worst_case_delay() == 10.0 + 15.0 * 3

    def test_is_zero_without_retries(self) -> None:
        assert NO_RETRY_POLICY.total_worst_case_delay() == 0.0


class TestPresets:
    def test_network_preset_is_patient_but_bounded(self) -> None:
        assert NETWORK_RETRY_POLICY.max_attempts == 5
        assert NETWORK_RETRY_POLICY.max_backoff_seconds == 8.0

    def test_rate_limit_preset_honours_provider_hints(self) -> None:
        assert RATE_LIMIT_RETRY_POLICY.respect_retry_after is True
        assert RATE_LIMIT_RETRY_POLICY.max_backoff_seconds == 60.0

    def test_presets_are_immutable(self) -> None:
        """Presets are module-level singletons shared by every agent and tool.

        If they were mutable, one caller tightening a preset would silently
        change retry behaviour everywhere else that referenced it.
        """
        with pytest.raises(ValidationError, match="frozen"):
            DEFAULT_RETRY_POLICY.max_attempts = 5  # type: ignore[misc]

    def test_variants_are_derived_by_copy(self) -> None:
        patient = DEFAULT_RETRY_POLICY.model_copy(update={"max_attempts": 7})
        assert patient.max_attempts == 7
        assert DEFAULT_RETRY_POLICY.max_attempts == 3, "the shared preset was mutated"
