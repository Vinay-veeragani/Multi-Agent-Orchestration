"""Tests for the benchmark judge: does it grade an observed run correctly.

Everything here is synthetic -- a hand-built :class:`ExecutionState` and
:class:`Workflow`, no engine execution -- because the judge's job is purely to
compare expectations against outcomes. What actually *produces* those outcomes
is exercised separately, against real infrastructure, in
``tests/integration/test_evaluation.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from orchestration.domain.base import utc_now
from orchestration.domain.enums import ExecutionStatus, NodeKind, SupervisorAction
from orchestration.domain.evaluation import BenchmarkScenario, ScenarioExpectation
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolResult
from orchestration.domain.workflow import Task, Workflow, WorkflowNode
from orchestration.evaluation.judge import invoked_agents, invoked_tools, judge, max_overlap

pytestmark = pytest.mark.unit


def _scenario(**expectation_kwargs: object) -> BenchmarkScenario:
    return BenchmarkScenario(
        id="scn-1",
        category="test",
        description="a test scenario",
        task="do the thing",
        expectation=ScenarioExpectation(**expectation_kwargs),  # type: ignore[arg-type]
    )


def _state(*, status: ExecutionStatus = ExecutionStatus.SUCCEEDED) -> ExecutionState:
    return ExecutionState(
        execution_id="exec_1", workflow_id="wkf_1", task=Task(description="do it"), status=status
    )


def _workflow_with(*agent_ids: str) -> Workflow:
    return Workflow(
        name="w",
        nodes=tuple(
            WorkflowNode(id=aid, kind=NodeKind.AGENT, agent_id=aid) for aid in agent_ids
        )
        or (WorkflowNode(id="t", kind=NodeKind.TERMINAL),),
    )


class TestStatusAndFirstAction:
    def test_matching_status_passes(self) -> None:
        scenario = _scenario(status=ExecutionStatus.SUCCEEDED)
        state = _state(status=ExecutionStatus.SUCCEEDED)
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed
        assert result.assertions["status"] is True

    def test_mismatched_status_fails_with_a_readable_reason(self) -> None:
        scenario = _scenario(status=ExecutionStatus.SUCCEEDED)
        state = _state(status=ExecutionStatus.FAILED)
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed
        assert "succeeded" in result.failures[0]
        assert "failed" in result.failures[0]

    def test_unasserted_status_is_absent_from_assertions(self) -> None:
        """A dimension the scenario never claimed must not count as a pass."""
        scenario = _scenario()
        state = _state(status=ExecutionStatus.FAILED)
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed
        assert "status" not in result.assertions

    def test_first_action_mismatch_fails(self) -> None:
        scenario = _scenario(first_action=SupervisorAction.DELEGATE)
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=_state(),
            tool_results=(),
            first_action=SupervisorAction.PARALLEL_DELEGATE,
            latency_seconds=0.01,
        )
        assert not result.passed
        assert result.assertions["first_action"] is False


class TestAgentsAndTools:
    def test_required_agent_present_passes(self) -> None:
        scenario = _scenario(required_agents=frozenset({"research_agent"}))
        state = _state()
        state.node_state("research_agent").mark_succeeded()
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with("research_agent"),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed

    def test_required_agent_missing_fails(self) -> None:
        scenario = _scenario(required_agents=frozenset({"research_agent"}))
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=_state(),
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed
        assert "research_agent" in result.failures[0]

    def test_an_agent_node_that_only_ran_but_did_not_succeed_does_not_count(self) -> None:
        """A node still in flight or failed was not usefully 'invoked'."""
        scenario = _scenario(required_agents=frozenset({"research_agent"}))
        state = _state()
        state.node_state("research_agent").mark_running()
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with("research_agent"),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed

    def test_forbidden_agent_invoked_fails(self) -> None:
        scenario = _scenario(forbidden_agents=frozenset({"critic_agent"}))
        state = _state()
        state.node_state("critic_agent").mark_succeeded()
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with("critic_agent"),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed

    def test_required_tool_must_have_actually_succeeded(self) -> None:
        scenario = _scenario(required_tools=frozenset({"calculator"}))
        denied = ToolResult(tool="calculator", ok=False, error_code="permission_denied")
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=_state(),
            tool_results=(denied,),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed
        assert "calculator" in result.failures[0]

    def test_forbidden_tool_that_was_denied_does_not_count_as_run(self) -> None:
        """The whole point of a deny-by-default test: a refused call must pass."""
        scenario = _scenario(forbidden_tools=frozenset({"exec_shell"}))
        denied = ToolResult(tool="exec_shell", ok=False, error_code="permission_denied")
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=_state(),
            tool_results=(denied,),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed

    def test_invoked_tools_helper_only_counts_successes(self) -> None:
        results = (
            ToolResult(tool="a", ok=True),
            ToolResult(tool="b", ok=False, error_code="x"),
        )
        assert invoked_tools(results) == frozenset({"a"})

    def test_invoked_agents_helper_reads_from_the_workflow_and_state_together(self) -> None:
        state = _state()
        state.node_state("a").mark_succeeded()
        state.node_state("b").mark_failed({"code": "x", "message": "y"})
        workflow = _workflow_with("a", "b")
        assert invoked_agents(workflow, state) == frozenset({"a"})


class TestOutputContains:
    def test_missing_substring_fails(self) -> None:
        scenario = _scenario(output_contains=("five vendors",))
        state = _state()
        state.final_output = "only two vendors found"
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed

    def test_present_substring_passes(self) -> None:
        scenario = _scenario(output_contains=("five vendors",))
        state = _state()
        state.final_output = "we found five vendors total"
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed


class TestRetryAndRecovery:
    def test_expects_retry_needs_a_nonzero_retry_count(self) -> None:
        scenario = _scenario(expects_retry=True)
        state = _state()
        result = judge(
            scenario,
            arm="supervisor-retry",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed

    def test_expects_recovery_needs_a_node_that_failed_then_succeeded(self) -> None:
        scenario = _scenario(expects_recovery=True)
        state = _state()
        node = state.node_state("research_agent")
        node.mark_running()
        node.mark_failed({"code": "timeout", "message": "x", "retryable": True})
        node.mark_running()
        node.mark_succeeded()
        result = judge(
            scenario,
            arm="supervisor-retry",
            workflow=_workflow_with("research_agent"),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed
        assert result.assertions["recovered"] is True


class TestApprovalBudgetAndSteps:
    def test_expects_approval_needs_a_nonempty_approvals_tuple(self) -> None:
        scenario = _scenario(expects_approval=True)
        state = _state()
        state.approvals = ("appr_1",)
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed

    def test_expects_budget_exceeded_checks_the_status(self) -> None:
        scenario = _scenario(expects_budget_exceeded=True)
        state = _state(status=ExecutionStatus.BUDGET_EXCEEDED)
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert result.passed

    def test_max_agent_steps_ceiling_is_enforced(self) -> None:
        scenario = _scenario(max_agent_steps=2)
        state = _state()
        state.budget_usage.agent_steps = 5
        result = judge(
            scenario,
            arm="supervisor",
            workflow=_workflow_with(),
            state=state,
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
        )
        assert not result.passed


class TestHarnessErrorSurfaces:
    def test_a_harness_error_always_fails_the_scenario_even_if_assertions_held(self) -> None:
        scenario = _scenario()
        result = judge(
            scenario,
            arm="baseline",
            workflow=_workflow_with(),
            state=_state(),
            tool_results=(),
            first_action=None,
            latency_seconds=0.01,
            error="ValueError: something broke",
        )
        assert not result.passed
        assert result.error == "ValueError: something broke"


class TestMaxOverlap:
    def test_no_timestamps_defaults_to_one(self) -> None:
        assert max_overlap(_state()) == 1

    def test_disjoint_nodes_do_not_overlap(self) -> None:
        state = _state()
        now = utc_now()
        a = state.node_state("a")
        a.started_at = now
        a.completed_at = now + timedelta(seconds=1)
        b = state.node_state("b")
        b.started_at = now + timedelta(seconds=2)
        b.completed_at = now + timedelta(seconds=3)
        assert max_overlap(state) == 1

    def test_concurrent_nodes_overlap(self) -> None:
        state = _state()
        now = utc_now()
        for node_id in ("a", "b", "c"):
            node = state.node_state(node_id)
            node.started_at = now
            node.completed_at = now + timedelta(seconds=1)
        assert max_overlap(state) == 3

    def test_a_shared_exact_boundary_counts_as_overlapping(self) -> None:
        """Mock-provider timing is near-instant; a tie must not read as serial."""
        state = _state()
        now = utc_now()
        a = state.node_state("a")
        a.started_at = now
        a.completed_at = now
        b = state.node_state("b")
        b.started_at = now
        b.completed_at = now
        assert max_overlap(state) == 2
