"""Turns a scenario's expectations plus an observed run into a verdict.

One function, :func:`judge`, checks each assertion the scenario actually
makes (unasserted dimensions are simply absent from the result, never counted
as a pass) and returns a complete :class:`~orchestration.domain.evaluation.
ScenarioResult`. Nothing here executes anything -- that is
:mod:`orchestration.evaluation.harness`'s job -- this module only grades what
already happened.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestration.domain.enums import ExecutionStatus, NodeStatus, SupervisorAction
from orchestration.domain.evaluation import BenchmarkScenario, ScenarioResult
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolResult
from orchestration.domain.workflow import Workflow


def invoked_agents(workflow: Workflow, state: ExecutionState) -> frozenset[str]:
    """Agents whose node actually reached SUCCEEDED.

    Reads the workflow rather than a separate observer: every arm's nodes
    (whether hand-authored or compiled by the dynamic orchestrator) carry
    ``agent_id``, so this needs no extra instrumentation threaded through the
    harness.
    """
    succeeded = {
        node_id
        for node_id, node in state.node_states.items()
        if node.status is NodeStatus.SUCCEEDED
    }
    return frozenset(
        node.agent_id for node in workflow.nodes if node.id in succeeded and node.agent_id
    )


def invoked_tools(tool_results: Sequence[ToolResult]) -> frozenset[str]:
    """Tools that actually ran, as opposed to being requested and denied."""
    return frozenset(r.tool for r in tool_results if r.ok)


def recovered_from_failure(state: ExecutionState) -> bool:
    """Whether any node failed at least once and still went on to succeed."""
    return any(
        node.attempts > 1 and node.status is NodeStatus.SUCCEEDED
        for node in state.node_states.values()
    )


def max_overlap(state: ExecutionState) -> int:
    """Peak count of nodes whose ``[started_at, completed_at)`` intervals overlap.

    Computed from timestamps rather than threaded through the executor: a
    dynamic run spans several :class:`WorkflowExecutor` instances (one per
    supervisor turn), so there is no single ``ExecutionResult.max_parallelism``
    covering the whole run -- but every node's own start/end time is already in
    the checkpointed state regardless of which turn produced it.
    """
    events: list[tuple[object, int]] = []
    for node in state.node_states.values():
        if node.started_at is None or node.completed_at is None:
            continue
        events.append((node.started_at, 1))
        events.append((node.completed_at, -1))
    if not events:
        return 1
    # Starts before ends at an exact tie: a mock provider completes near
    # instantly, so two genuinely concurrent nodes can easily share a
    # microsecond-resolution timestamp for both their start and their finish.
    # Counting the tie as non-overlapping would make real parallelism
    # invisible to a benchmark run purely against the mock provider.
    events.sort(key=lambda e: (e[0], -e[1]))
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return max(peak, 1)


def judge(
    scenario: BenchmarkScenario,
    *,
    arm: str,
    workflow: Workflow,
    state: ExecutionState,
    tool_results: Sequence[ToolResult],
    first_action: SupervisorAction | None,
    latency_seconds: float,
    error: str | None = None,
) -> ScenarioResult:
    """Grade one observed run against ``scenario.expectation``."""
    expectation = scenario.expectation
    assertions: dict[str, bool] = {}
    failures: list[str] = []

    def _assert(name: str, held: bool, detail: str) -> None:
        assertions[name] = held
        if not held:
            failures.append(detail)

    if expectation.status is not None:
        _assert(
            "status",
            state.status is expectation.status,
            f"expected status {expectation.status.value!r}, got {state.status.value!r}",
        )

    if expectation.first_action is not None:
        _assert(
            "first_action",
            first_action is expectation.first_action,
            f"expected first action {expectation.first_action.value!r}, "
            f"got {first_action.value if first_action else None!r}",
        )

    agents_used = invoked_agents(workflow, state)
    if expectation.required_agents:
        missing = expectation.required_agents - agents_used
        _assert(
            "required_agents",
            not missing,
            f"required agents never invoked: {sorted(missing)}",
        )
    if expectation.forbidden_agents:
        present = expectation.forbidden_agents & agents_used
        _assert(
            "forbidden_agents",
            not present,
            f"forbidden agents were invoked: {sorted(present)}",
        )

    tools_used = invoked_tools(tool_results)
    if expectation.required_tools:
        missing_tools = expectation.required_tools - tools_used
        _assert(
            "required_tools",
            not missing_tools,
            f"required tools never ran: {sorted(missing_tools)}",
        )
    if expectation.forbidden_tools:
        present_tools = expectation.forbidden_tools & tools_used
        _assert(
            "forbidden_tools",
            not present_tools,
            f"forbidden tools ran: {sorted(present_tools)}",
        )

    validated_calls = [r for r in tool_results if r.error_code != "validation_error"]
    if tool_results:
        assertions["tool_arguments_valid"] = len(validated_calls) == len(tool_results)

    if expectation.output_contains:
        output = state.final_output or ""
        missing_substrings = [s for s in expectation.output_contains if s not in output]
        _assert(
            "output_contains",
            not missing_substrings,
            f"final output missing expected substrings: {missing_substrings}",
        )

    if expectation.expects_retry:
        _assert(
            "retry",
            state.budget_usage.retries > 0,
            "expected at least one retry, none occurred",
        )

    if expectation.expects_recovery:
        assertions["recovered"] = recovered_from_failure(state)
        if not assertions["recovered"]:
            failures.append("expected a failed node to recover, none did")

    if expectation.expects_approval:
        _assert(
            "approval",
            bool(state.approvals),
            "expected an approval to be raised, none was",
        )

    max_parallelism = max_overlap(state)
    if expectation.expects_parallelism:
        _assert(
            "parallelism",
            max_parallelism >= 2,
            f"expected overlapping agents, peak concurrency was {max_parallelism}",
        )

    if expectation.expects_budget_exceeded:
        _assert(
            "budget_exceeded",
            state.status is ExecutionStatus.BUDGET_EXCEEDED,
            f"expected budget exhaustion, status was {state.status.value!r}",
        )

    if expectation.max_agent_steps is not None:
        _assert(
            "max_agent_steps",
            state.budget_usage.agent_steps <= expectation.max_agent_steps,
            f"agent steps {state.budget_usage.agent_steps} exceeded "
            f"the {expectation.max_agent_steps} cap",
        )

    if error is not None:
        failures.append(f"scenario harness error: {error}")

    return ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        arm=arm,
        passed=not failures,
        execution_id=state.execution_id,
        final_status=state.status,
        assertions=assertions,
        failures=tuple(failures),
        agent_steps=state.budget_usage.agent_steps,
        tool_calls=state.budget_usage.tool_calls,
        retries=state.budget_usage.retries,
        total_tokens=state.budget_usage.total_tokens,
        cost_usd=state.budget_usage.cost_usd,
        latency_seconds=latency_seconds,
        max_parallelism=max_parallelism,
        error=error,
    )
