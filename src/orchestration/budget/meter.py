"""Budget enforcement.

:class:`BudgetMeter` is the only place in the engine that decides whether work
may proceed. Every agent invocation, tool call and LLM request consults it
*before* spending, which is the difference between a budget and a report.

Two mechanisms, and the distinction matters:

**Check** (:meth:`BudgetMeter.check`)
    "Am I already over?" Consulted before starting work. Cheap, and the common
    case.

**Reserve/commit** (:meth:`BudgetMeter.reserve`)
    "Will this specific spend put me over?" Used where the cost is knowable in
    advance -- notably a fan-out, where committing to five parallel branches at
    once can blow a budget that no single branch would have. Reserving up front
    means the engine refuses the fifth branch rather than discovering the overrun
    after all five have run.

The meter is deliberately synchronous and holds no I/O. It is fed usage by the
executor and its state lives in :class:`BudgetUsage`, which is checkpointed --
so a resumed execution continues with the budget it had already consumed rather
than a fresh allowance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orchestration.domain.budget import (
    Budget,
    BudgetSnapshot,
    BudgetStatus,
    BudgetUsage,
)
from orchestration.domain.enums import BudgetDimension
from orchestration.errors import BudgetExceededError


@dataclass(slots=True)
class Reservation:
    """A tentative spend, held until committed or released.

    Reservations exist so a fan-out can be sized before it starts. They are not
    a transaction: releasing one restores the headroom, committing one folds it
    into actual usage.
    """

    id: str
    agent_steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    reason: str = ""


class BudgetMeter:
    """Enforces a :class:`Budget` against live :class:`BudgetUsage`.

    Args:
        budget: The limits.
        usage: The running tally. Mutated in place, so the caller's checkpointed
            state stays authoritative -- the meter deliberately does not own a
            private copy that could drift from what gets persisted.
        elapsed: Supplier of elapsed seconds. Injected because the meter holds no
            clock: duration is derived from the execution's start timestamp so a
            resumed run cannot reset its time allowance.
        on_warning: Called once per dimension when it crosses the warn fraction.
    """

    def __init__(
        self,
        budget: Budget,
        usage: BudgetUsage,
        *,
        elapsed: Callable[[], float] = lambda: 0.0,
        on_warning: Callable[[BudgetStatus], None] | None = None,
    ) -> None:
        self._budget = budget
        self._usage = usage
        self._elapsed = elapsed
        self._on_warning = on_warning
        self._reservations: dict[str, Reservation] = {}
        self._warned: set[BudgetDimension] = set()

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    # -- inspection --------------------------------------------------------

    def status_for(self, dimension: BudgetDimension) -> BudgetStatus:
        """Current position on one dimension, including reserved-but-unspent."""
        limit = self._budget.limit_for(dimension)
        used = self._usage.value_for(dimension, elapsed_seconds=self._elapsed())
        used += self._reserved_for(dimension)
        exceeded = limit is not None and used > limit
        warning = (
            limit is not None and not exceeded and used >= limit * self._budget.warn_at_fraction
        )
        return BudgetStatus(
            dimension=dimension, used=used, limit=limit, exceeded=exceeded, warning=warning
        )

    def snapshot(self) -> BudgetSnapshot:
        """Full position, for API responses, events, and the supervisor prompt."""
        return BudgetSnapshot(
            usage=self._usage,
            statuses=tuple(self.status_for(d) for d in BudgetDimension),
            elapsed_seconds=self._elapsed(),
        )

    def remaining(self, dimension: BudgetDimension) -> float | None:
        return self.status_for(dimension).remaining

    @property
    def is_exhausted(self) -> bool:
        return any(self.status_for(d).exceeded for d in BudgetDimension)

    def exceeded_dimensions(self) -> tuple[BudgetStatus, ...]:
        return tuple(s for d in BudgetDimension if (s := self.status_for(d)).exceeded)

    # -- enforcement -------------------------------------------------------

    def check(self, reason: str = "") -> None:
        """Raise if any hard limit has been reached.

        Raises:
            BudgetExceededError: Naming the dimension, its limit, and the
                consumption -- so the API can return a clear reason rather than a
                generic failure.
        """
        exceeded = self.exceeded_dimensions()
        if exceeded:
            first = exceeded[0]
            assert first.limit is not None
            raise BudgetExceededError(
                f"budget exhausted on {first.dimension.value}",
                dimension=first.dimension.value,
                limit=first.limit,
                used=first.used,
                reason=reason,
                all_exceeded=[s.dimension.value for s in exceeded],
            )
        self._emit_warnings()

    def would_exceed(
        self,
        *,
        agent_steps: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> BudgetStatus | None:
        """The first dimension a prospective spend would breach, or ``None``."""
        prospective: dict[BudgetDimension, float] = {
            BudgetDimension.AGENT_STEPS: agent_steps,
            BudgetDimension.TOOL_CALLS: tool_calls,
            BudgetDimension.TOKENS: tokens,
            BudgetDimension.COST_USD: cost_usd,
        }
        for dimension, delta in prospective.items():
            if delta <= 0:
                continue
            status = self.status_for(dimension)
            if status.limit is not None and status.used + delta > status.limit:
                return status
        return None

    def reserve(
        self,
        reservation_id: str,
        *,
        agent_steps: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
        reason: str = "",
    ) -> Reservation:
        """Hold headroom for a known-size spend.

        Raises:
            BudgetExceededError: If the reservation cannot fit. Refusing here is
                the point: a five-way fan-out that would overrun is rejected
                before any branch starts, rather than after all five have run.
        """
        breach = self.would_exceed(
            agent_steps=agent_steps, tool_calls=tool_calls, tokens=tokens, cost_usd=cost_usd
        )
        if breach is not None:
            assert breach.limit is not None
            raise BudgetExceededError(
                f"reservation would exhaust {breach.dimension.value}",
                dimension=breach.dimension.value,
                limit=breach.limit,
                used=breach.used,
                reason=reason,
                reservation=reservation_id,
            )
        reservation = Reservation(
            id=reservation_id,
            agent_steps=agent_steps,
            tool_calls=tool_calls,
            tokens=tokens,
            cost_usd=cost_usd,
            reason=reason,
        )
        self._reservations[reservation_id] = reservation
        return reservation

    def release(self, reservation_id: str) -> None:
        """Drop a reservation without spending it (a cancelled branch)."""
        self._reservations.pop(reservation_id, None)

    def commit(self, reservation_id: str) -> None:
        """Convert a reservation into actual usage.

        The reserved amounts are *not* added: the executor records real usage as
        it happens, so adding the reservation too would double count. Commit only
        releases the hold.
        """
        self._reservations.pop(reservation_id, None)

    def fit_parallel_branches(self, requested: int, *, cost_per_branch: int = 1) -> int:
        """How many parallel branches the remaining agent-step allowance permits.

        Returns a count rather than raising, so the executor can run a narrower
        fan-out instead of failing the whole step -- three of five branches is
        usually far more useful than none.
        """
        status = self.status_for(BudgetDimension.AGENT_STEPS)
        if status.limit is None:
            return requested
        remaining = max(0.0, status.limit - status.used)
        return max(0, min(requested, int(remaining // max(1, cost_per_branch))))

    # -- recording ---------------------------------------------------------

    def record_agent_step(self, count: int = 1) -> None:
        self._usage.agent_steps += count

    def record_tool_call(self, count: int = 1) -> None:
        self._usage.tool_calls += count

    def record_retry(self, count: int = 1) -> None:
        self._usage.retries += count

    def record_llm_usage(self, *, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self._usage.add_llm_usage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
        )

    # -- internals ---------------------------------------------------------

    def _reserved_for(self, dimension: BudgetDimension) -> float:
        match dimension:
            case BudgetDimension.AGENT_STEPS:
                return float(sum(r.agent_steps for r in self._reservations.values()))
            case BudgetDimension.TOOL_CALLS:
                return float(sum(r.tool_calls for r in self._reservations.values()))
            case BudgetDimension.TOKENS:
                return float(sum(r.tokens for r in self._reservations.values()))
            case BudgetDimension.COST_USD:
                return sum(r.cost_usd for r in self._reservations.values())
            case _:
                return 0.0

    def _emit_warnings(self) -> None:
        """Fire the warning callback once per dimension.

        Once, not repeatedly: a dimension sitting at 85% for twenty steps would
        otherwise produce twenty identical events and bury everything else.
        """
        if self._on_warning is None:
            return
        for dimension in BudgetDimension:
            if dimension in self._warned:
                continue
            status = self.status_for(dimension)
            if status.warning:
                self._warned.add(dimension)
                self._on_warning(status)


@dataclass(slots=True)
class BudgetGuard:
    """Adapter exposing a meter as the async callback the agent runtime expects.

    The runtime takes a ``BudgetCheck`` callable rather than a meter, so it can be
    tested without one. This is the small piece of glue that connects them.
    """

    meter: BudgetMeter
    #: Reasons recorded, for assertions and debugging.
    checks: list[str] = field(default_factory=list)

    async def __call__(self, reason: str) -> None:
        self.checks.append(reason)
        self.meter.check(reason)


def build_meter(
    budget: Budget,
    usage: BudgetUsage,
    *,
    elapsed: Callable[[], float] = lambda: 0.0,
    on_warning: Callable[[BudgetStatus], None] | None = None,
) -> BudgetMeter:
    """Convenience constructor."""
    return BudgetMeter(budget, usage, elapsed=elapsed, on_warning=on_warning)
