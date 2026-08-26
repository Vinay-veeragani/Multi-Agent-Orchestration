"""Evaluation and benchmark result models.

These types exist so benchmark output is structured data rather than printed
text. Every number the README reports is produced by serialising these models
from a real run, which is what makes the results auditable instead of asserted.

Latency here is *measured wall-clock of the engine*. When the mock provider is
in use, LLM time is synthetic and :attr:`BenchmarkReport.provider_note` records
that fact so a reader cannot mistake these figures for real provider latency.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime

from pydantic import Field, model_validator

from orchestration.domain.base import (
    BoundedText,
    DomainModel,
    FrozenModel,
    JsonDict,
    Score,
    Slug,
    id_factory,
    utc_now,
)
from orchestration.domain.enums import ExecutionStatus, SupervisorAction


class ScenarioExpectation(FrozenModel):
    """What a benchmark scenario asserts about correct behaviour.

    Every field is optional: a scenario asserts only what it is actually testing,
    and unasserted dimensions are excluded from that scenario's accuracy
    denominator rather than counted as passes.
    """

    #: Terminal execution status the run should reach.
    status: ExecutionStatus | None = None
    #: The supervisor's first action.
    first_action: SupervisorAction | None = None
    #: Agents that must be invoked (subset check, order-insensitive).
    required_agents: frozenset[str] = Field(default_factory=frozenset)
    #: Agents that must *not* be invoked.
    forbidden_agents: frozenset[str] = Field(default_factory=frozenset)
    #: Tools that must be invoked.
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    #: Tools that must not be invoked -- the permission-enforcement assertion.
    forbidden_tools: frozenset[str] = Field(default_factory=frozenset)
    #: Substrings that must appear in the final output.
    output_contains: tuple[str, ...] = ()
    #: Whether the run must involve at least one retry.
    expects_retry: bool = False
    #: Whether the run must pause for human approval.
    expects_approval: bool = False
    #: Whether at least two agents must have overlapped in time.
    expects_parallelism: bool = False
    #: Whether a budget limit must trip.
    expects_budget_exceeded: bool = False
    #: Whether a failure must be recovered from.
    expects_recovery: bool = False
    #: Upper bound on agent invocations, to catch pathological over-delegation.
    max_agent_steps: int | None = Field(default=None, gt=0)


class BenchmarkScenario(DomainModel):
    """One deterministic benchmark case.

    Determinism comes from ``mock_script`` (the exact LLM replies to serve) and
    ``fault_injection`` (the exact failures to raise). Given the same scenario
    the engine must produce the same trajectory on every run, which is what makes
    regression detection meaningful.
    """

    id: Slug
    category: Slug
    description: str = Field(min_length=1, max_length=1_000)
    task: BoundedText = Field(min_length=1)
    inputs: JsonDict = Field(default_factory=dict)
    #: Workflow to run; ``None`` means dynamic supervisor-driven execution.
    workflow_ref: str | None = None
    expectation: ScenarioExpectation = Field(default_factory=ScenarioExpectation)
    #: Scripted mock-provider behaviour, keyed by request role.
    mock_script: JsonDict = Field(default_factory=dict)
    #: Faults to inject, e.g. {"research_agent": {"attempts": [1], "error": "timeout"}}.
    fault_injection: JsonDict = Field(default_factory=dict)
    budget_override: JsonDict | None = None
    #: Approval decisions to apply automatically, so the run is unattended.
    auto_approve: bool = False
    auto_reject: bool = False
    tags: frozenset[str] = Field(default_factory=frozenset)
    weight: float = Field(default=1.0, gt=0)


class ScenarioResult(FrozenModel):
    """Outcome of running one scenario, with per-assertion detail."""

    scenario_id: Slug
    category: Slug
    arm: Slug
    passed: bool
    execution_id: str | None = None
    final_status: ExecutionStatus | None = None
    #: Assertion name -> whether it held. Absent assertions were not tested.
    assertions: dict[str, bool] = Field(default_factory=dict)
    failures: tuple[str, ...] = ()
    agent_steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0)
    #: Peak number of concurrently running agents observed.
    max_parallelism: int = Field(default=1, ge=0)
    error: str | None = None

    @property
    def assertion_pass_rate(self) -> float | None:
        if not self.assertions:
            return None
        return round(sum(self.assertions.values()) / len(self.assertions), 6)


class ArmMetrics(FrozenModel):
    """Aggregate metrics for one configuration arm across all scenarios."""

    arm: Slug
    scenarios_run: int = Field(ge=0)
    scenarios_passed: int = Field(ge=0)
    #: Accuracy of the supervisor's first action, over scenarios that assert it.
    routing_accuracy: float | None = None
    #: Fraction of scenarios whose required/forbidden tool assertions all held.
    tool_selection_accuracy: float | None = None
    #: Fraction of tool calls whose arguments validated against the input schema.
    tool_argument_validity: float | None = None
    #: Of scenarios expecting recovery, the fraction that recovered.
    recovery_success_rate: float | None = None
    avg_agent_steps: float = 0.0
    avg_latency_seconds: float = 0.0
    p50_latency_seconds: float = 0.0
    p95_latency_seconds: float = 0.0
    p99_latency_seconds: float = 0.0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    avg_max_parallelism: float = 1.0

    @property
    def task_completion_rate(self) -> float:
        if self.scenarios_run == 0:
            return 0.0
        return round(self.scenarios_passed / self.scenarios_run, 6)


def percentile(values: Sequence[float], q: float) -> float:
    """Percentile with linear interpolation.

    Implemented directly rather than pulled from numpy: this is the only
    statistical function the benchmark needs, and adding a numeric stack as a
    runtime dependency for one formula is not a trade worth making.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 6)


def summarise_arm(arm: str, results: Sequence[ScenarioResult]) -> ArmMetrics:
    """Aggregate per-scenario results into :class:`ArmMetrics`.

    Rates whose denominator is zero are reported as ``None``, never as ``0.0``.
    "No scenario tested this" and "every scenario failed this" are different
    claims, and conflating them would misreport the benchmark.
    """
    if not results:
        return ArmMetrics(arm=arm, scenarios_run=0, scenarios_passed=0)

    latencies = [r.latency_seconds for r in results]

    routing_judged = [r for r in results if "first_action" in r.assertions]
    tool_judged = [
        r
        for r in results
        if any(k.startswith(("required_tools", "forbidden_tools")) for k in r.assertions)
    ]
    validity_judged = [r for r in results if "tool_arguments_valid" in r.assertions]
    recovery_judged = [r for r in results if "recovered" in r.assertions]

    def _rate(subset: Sequence[ScenarioResult], key_prefix: str) -> float | None:
        if not subset:
            return None
        held = sum(
            1 for r in subset if all(v for k, v in r.assertions.items() if k.startswith(key_prefix))
        )
        return round(held / len(subset), 6)

    return ArmMetrics(
        arm=arm,
        scenarios_run=len(results),
        scenarios_passed=sum(1 for r in results if r.passed),
        routing_accuracy=_rate(routing_judged, "first_action"),
        tool_selection_accuracy=_rate(tool_judged, "required_tools") if tool_judged else None,
        tool_argument_validity=_rate(validity_judged, "tool_arguments_valid"),
        recovery_success_rate=_rate(recovery_judged, "recovered"),
        avg_agent_steps=round(statistics.fmean(r.agent_steps for r in results), 4),
        avg_latency_seconds=round(statistics.fmean(latencies), 6),
        p50_latency_seconds=percentile(latencies, 0.50),
        p95_latency_seconds=percentile(latencies, 0.95),
        p99_latency_seconds=percentile(latencies, 0.99),
        total_cost_usd=round(sum(r.cost_usd for r in results), 8),
        total_tokens=sum(r.total_tokens for r in results),
        avg_max_parallelism=round(statistics.fmean(r.max_parallelism for r in results), 4),
    )


class EvaluationResult(FrozenModel):
    """Result of evaluating one execution against expectations.

    Used both by the benchmark and by the critic agent's self-evaluation path.
    """

    id: str = Field(default_factory=id_factory("evaluation"))
    execution_id: str
    scenario_id: Slug | None = None
    passed: bool
    score: Score = 0.0
    assertions: dict[str, bool] = Field(default_factory=dict)
    failures: tuple[str, ...] = ()
    notes: BoundedText = ""
    created_at: datetime = Field(default_factory=utc_now)


class BenchmarkReport(FrozenModel):
    """A complete benchmark run, ready to serialise to ``benchmarks/results``."""

    id: str = Field(default_factory=id_factory("evaluation"))
    started_at: datetime
    completed_at: datetime
    #: Git commit the run was produced from, for reproducibility.
    git_sha: str | None = None
    #: Host and interpreter details -- latency is meaningless without them.
    environment: JsonDict = Field(default_factory=dict)
    #: States plainly whether LLM latency was synthetic (mock) or real.
    provider_note: str = Field(min_length=1)
    scenario_count: int = Field(ge=0)
    arms: tuple[ArmMetrics, ...] = ()
    results: tuple[ScenarioResult, ...] = ()

    @model_validator(mode="after")
    def _arms_are_unique(self):  # type: ignore[no-untyped-def]
        names = [a.arm for a in self.arms]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate arms in benchmark report: {duplicates}")
        return self

    @property
    def duration_seconds(self) -> float:
        return round((self.completed_at - self.started_at).total_seconds(), 3)

    def arm(self, name: str) -> ArmMetrics | None:
        return next((a for a in self.arms if a.arm == name), None)

    def results_for(self, arm: str) -> tuple[ScenarioResult, ...]:
        return tuple(r for r in self.results if r.arm == arm)

    def by_category(self, arm: str) -> dict[str, ArmMetrics]:
        """Per-category breakdown for one arm."""
        buckets: dict[str, list[ScenarioResult]] = {}
        for r in self.results_for(arm):
            buckets.setdefault(r.category, []).append(r)
        return {cat: summarise_arm(cat, rs) for cat, rs in sorted(buckets.items())}
