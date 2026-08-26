"""Retry policy value object.

The policy is pure data plus pure functions: given an attempt number and an
exception it says *whether* to retry and *how long* to wait. It performs no
sleeping and no I/O, which is what makes backoff arithmetic unit-testable
without a clock or an event loop.
"""

from __future__ import annotations

import hashlib
import random
from typing import Literal, Self

from pydantic import Field, model_validator

from orchestration.domain.base import FrozenModel
from orchestration.errors import RateLimitError, is_retryable


class RetryPolicy(FrozenModel):
    """Declarative retry configuration.

    Immutable: the module-level presets below are shared by every agent and tool
    that references them, so allowing assignment would let one caller silently
    change another's retry behaviour. Use ``model_copy(update=...)`` to derive a
    variant.

    Attributes:
        max_attempts: Total attempts including the first. ``1`` disables retry.
        initial_backoff_seconds: Delay before the second attempt.
        max_backoff_seconds: Ceiling for the computed delay.
        multiplier: Exponential growth factor per attempt.
        jitter: Randomisation strategy. ``full`` jitter (a uniform draw over the
            whole window) is the default because it spreads a thundering herd
            better than an equal split, at the cost of less predictable delays.
        retry_on: Optional allowlist of error codes. When set, only these codes
            are retried even if the exception claims to be retryable.
        never_retry_on: Error codes that are never retried, overriding
            everything else.
        respect_retry_after: Honour a provider-supplied ``retry_after`` hint
            when it exceeds the computed backoff.
    """

    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_backoff_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    max_backoff_seconds: float = Field(default=30.0, ge=0.0, le=600.0)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    jitter: Literal["none", "full", "equal"] = "full"
    retry_on: frozenset[str] = Field(default_factory=frozenset)
    never_retry_on: frozenset[str] = Field(default_factory=frozenset)
    respect_retry_after: bool = True

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> Self:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= initial_backoff_seconds "
                f"(got {self.max_backoff_seconds} < {self.initial_backoff_seconds})"
            )
        overlap = self.retry_on & self.never_retry_on
        if overlap:
            raise ValueError(
                f"error codes appear in both retry_on and never_retry_on: {sorted(overlap)}"
            )
        return self

    # -- decisions ---------------------------------------------------------

    @property
    def max_retries(self) -> int:
        """Number of *re*-attempts, i.e. attempts after the first."""
        return self.max_attempts - 1

    def should_retry(self, attempt: int, exc: BaseException) -> bool:
        """Whether attempt number ``attempt`` (1-based) may be retried.

        Order of checks matters: the explicit ``never_retry_on`` denylist wins
        over an exception that claims retryability, so an operator can pin down
        a misbehaving error class without changing code.
        """
        from orchestration.errors import error_code  # local import: avoids cycle at module load

        if attempt >= self.max_attempts:
            return False
        code = error_code(exc)
        if code in self.never_retry_on:
            return False
        if self.retry_on:
            return code in self.retry_on
        return is_retryable(exc)

    def backoff_for(
        self,
        attempt: int,
        exc: BaseException | None = None,
        *,
        rng: random.Random | None = None,
    ) -> float:
        """Seconds to wait before the attempt following ``attempt`` (1-based).

        Args:
            attempt: The attempt that just failed.
            exc: The failure, consulted for a ``retry_after`` hint.
            rng: Random source. Injectable so tests are deterministic.
        """
        base = min(
            self.initial_backoff_seconds * (self.multiplier ** max(0, attempt - 1)),
            self.max_backoff_seconds,
        )
        delay = self._apply_jitter(base, rng)

        if (
            self.respect_retry_after
            and isinstance(exc, RateLimitError)
            and exc.retry_after is not None
        ):
            # A provider telling us when to come back is better information than
            # our own guess -- but still clamped, so a hostile or buggy header
            # cannot stall an execution indefinitely.
            delay = max(delay, min(exc.retry_after, self.max_backoff_seconds))

        return round(delay, 4)

    def _apply_jitter(self, base: float, rng: random.Random | None) -> float:
        if self.jitter == "none" or base == 0.0:
            return base
        source = rng or random
        if self.jitter == "full":
            return source.uniform(0.0, base)
        # "equal": half fixed, half random -- bounded worst case, still spread.
        half = base / 2.0
        return half + source.uniform(0.0, half)

    def deterministic_backoff_for(self, attempt: int, seed: str) -> float:
        """Reproducible backoff, derived from ``seed`` instead of global entropy.

        Used by the benchmark harness so that retry timings -- and therefore
        measured latencies -- are identical across runs.
        """
        digest = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
        rng = random.Random(draw)  # noqa: S311 - reproducibility, not cryptography
        return self.backoff_for(attempt, rng=rng)

    def total_worst_case_delay(self) -> float:
        """Upper bound on cumulative backoff, ignoring execution time.

        Lets the budget layer reason about whether a retry chain can even fit
        inside the remaining duration allowance before starting it.
        """
        return round(
            sum(
                min(
                    self.initial_backoff_seconds * (self.multiplier**i),
                    self.max_backoff_seconds,
                )
                for i in range(self.max_retries)
            ),
            4,
        )


#: Sensible default for agent execution.
DEFAULT_RETRY_POLICY = RetryPolicy()

#: No retries -- for side-effecting operations that must not be repeated blindly.
NO_RETRY_POLICY = RetryPolicy(max_attempts=1)

#: Tolerant policy for flaky network reads.
NETWORK_RETRY_POLICY = RetryPolicy(
    max_attempts=5,
    initial_backoff_seconds=0.25,
    max_backoff_seconds=8.0,
    multiplier=2.0,
    jitter="full",
)

#: Patient policy for provider rate limits.
RATE_LIMIT_RETRY_POLICY = RetryPolicy(
    max_attempts=4,
    initial_backoff_seconds=2.0,
    max_backoff_seconds=60.0,
    multiplier=3.0,
    jitter="equal",
    respect_retry_after=True,
)
