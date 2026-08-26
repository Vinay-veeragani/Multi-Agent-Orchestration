"""Execution budget and usage accounting.

Design note: :class:`Budget` (the limits) and :class:`BudgetUsage` (the
consumption) are separate objects. The budget is immutable configuration that
can be attached to an agent, a workflow, or a single execution; usage is the
mutable tally that lives in execution state and is checkpointed with it. Keeping
them apart means a checkpoint restores *consumption* without re-deriving limits,
and limits can be tightened between runs without rewriting history.

Enforcement lives in :mod:`orchestration.budget.meter`; this module is pure data
plus the comparison logic.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from orchestration.domain.base import DomainModel, FrozenModel
from orchestration.domain.enums import BudgetDimension


class Budget(FrozenModel):
    """Hard ceilings for one execution.

    Every dimension is optional: ``None`` means unmetered. All limits are *hard*
    -- when one is reached the execution stops with
    :class:`~orchestration.errors.BudgetExceededError`. A soft warning threshold
    is expressed as a fraction via :attr:`warn_at_fraction`, which emits a
    ``BUDGET_WARNING`` event without halting anything.
    """

    max_cost_usd: float | None = Field(default=0.50, gt=0)
    max_tokens: int | None = Field(default=50_000, gt=0)
    max_duration_seconds: float | None = Field(default=300.0, gt=0)
    max_agent_steps: int | None = Field(default=30, gt=0)
    max_tool_calls: int | None = Field(default=60, gt=0)
    max_retries: int | None = Field(default=10, ge=0)
    warn_at_fraction: float = Field(default=0.8, gt=0.0, le=1.0)

    def limit_for(self, dimension: BudgetDimension) -> float | None:
        """The configured ceiling for ``dimension``, or ``None`` if unmetered."""
        match dimension:
            case BudgetDimension.COST_USD:
                return self.max_cost_usd
            case BudgetDimension.TOKENS:
                return None if self.max_tokens is None else float(self.max_tokens)
            case BudgetDimension.DURATION_SECONDS:
                return self.max_duration_seconds
            case BudgetDimension.AGENT_STEPS:
                return None if self.max_agent_steps is None else float(self.max_agent_steps)
            case BudgetDimension.TOOL_CALLS:
                return None if self.max_tool_calls is None else float(self.max_tool_calls)
            case BudgetDimension.RETRIES:
                return None if self.max_retries is None else float(self.max_retries)

    @property
    def metered_dimensions(self) -> tuple[BudgetDimension, ...]:
        """Dimensions that actually have a ceiling configured."""
        return tuple(d for d in BudgetDimension if self.limit_for(d) is not None)

    def tightened_to(self, other: Budget) -> Budget:
        """Return the element-wise stricter of two budgets.

        Used when an agent-level budget must be reconciled with the enclosing
        execution budget: an agent may never widen what the execution allows.
        """

        def _min(a: float | None, b: float | None) -> float | None:
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        def _min_int(a: int | None, b: int | None) -> int | None:
            merged = _min(None if a is None else float(a), None if b is None else float(b))
            return None if merged is None else int(merged)

        return Budget(
            max_cost_usd=_min(self.max_cost_usd, other.max_cost_usd),
            max_tokens=_min_int(self.max_tokens, other.max_tokens),
            max_duration_seconds=_min(self.max_duration_seconds, other.max_duration_seconds),
            max_agent_steps=_min_int(self.max_agent_steps, other.max_agent_steps),
            max_tool_calls=_min_int(self.max_tool_calls, other.max_tool_calls),
            max_retries=_min_int(self.max_retries, other.max_retries),
            warn_at_fraction=min(self.warn_at_fraction, other.warn_at_fraction),
        )


class BudgetUsage(DomainModel):
    """Running consumption tally for one execution.

    Mutable and checkpointed. ``duration_seconds`` is not tracked here because
    it is derived from the execution start time rather than accumulated -- a
    resumed execution must not lose the wall-clock already spent, and deriving
    it from timestamps makes that automatic.
    """

    cost_usd: float = Field(default=0.0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    agent_steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def value_for(self, dimension: BudgetDimension, *, elapsed_seconds: float = 0.0) -> float:
        """Current consumption for ``dimension``.

        Args:
            elapsed_seconds: Supplied by the caller for the duration dimension,
                since usage itself holds no clock.
        """
        match dimension:
            case BudgetDimension.COST_USD:
                return self.cost_usd
            case BudgetDimension.TOKENS:
                return float(self.total_tokens)
            case BudgetDimension.DURATION_SECONDS:
                return elapsed_seconds
            case BudgetDimension.AGENT_STEPS:
                return float(self.agent_steps)
            case BudgetDimension.TOOL_CALLS:
                return float(self.tool_calls)
            case BudgetDimension.RETRIES:
                return float(self.retries)

    def add_llm_usage(self, *, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        """Record one LLM call."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd = round(self.cost_usd + cost_usd, 8)
        self.llm_calls += 1

    def merge(self, other: BudgetUsage) -> BudgetUsage:
        """Sum two usage tallies (used to fold parallel branch results in)."""
        return BudgetUsage(
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            agent_steps=self.agent_steps + other.agent_steps,
            tool_calls=self.tool_calls + other.tool_calls,
            retries=self.retries + other.retries,
            llm_calls=self.llm_calls + other.llm_calls,
        )


class BudgetStatus(FrozenModel):
    """Snapshot of one dimension checked against its limit."""

    dimension: BudgetDimension
    used: float
    limit: float | None
    exceeded: bool
    warning: bool

    @property
    def remaining(self) -> float | None:
        if self.limit is None:
            return None
        return max(0.0, self.limit - self.used)

    @property
    def fraction_used(self) -> float | None:
        if self.limit is None or self.limit == 0:
            return None
        return round(self.used / self.limit, 6)


class BudgetSnapshot(FrozenModel):
    """Full budget position at a moment in time, for API and event payloads."""

    usage: BudgetUsage
    statuses: tuple[BudgetStatus, ...]
    elapsed_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _require_unique_dimensions(self) -> Self:
        seen = [s.dimension for s in self.statuses]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate dimensions in budget snapshot")
        return self

    @property
    def exceeded(self) -> tuple[BudgetStatus, ...]:
        return tuple(s for s in self.statuses if s.exceeded)

    @property
    def warnings(self) -> tuple[BudgetStatus, ...]:
        return tuple(s for s in self.statuses if s.warning and not s.exceeded)

    @property
    def is_exhausted(self) -> bool:
        return bool(self.exceeded)


#: Generous default used when a caller supplies no budget.
DEFAULT_BUDGET = Budget()

#: Small budget used by benchmark scenarios that must trip a limit deliberately.
TIGHT_BUDGET = Budget(
    max_cost_usd=0.01,
    max_tokens=2_000,
    max_duration_seconds=15.0,
    max_agent_steps=3,
    max_tool_calls=5,
    max_retries=1,
)

#: Effectively unmetered -- for local experimentation only.
UNLIMITED_BUDGET = Budget(
    max_cost_usd=None,
    max_tokens=None,
    max_duration_seconds=None,
    max_agent_steps=None,
    max_tool_calls=None,
    max_retries=None,
)
