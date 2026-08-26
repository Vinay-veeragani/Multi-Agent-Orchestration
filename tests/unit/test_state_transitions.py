"""Tests for execution state, node bookkeeping, and the transition table.

The properties that matter most here are the ones checkpoint/resume depends on:
state must round-trip losslessly, attempt counts must survive, and terminal
statuses must be genuinely terminal.
"""

from __future__ import annotations

import pytest

from orchestration.domain import (
    Artifact,
    ExecutionError,
    ExecutionState,
    ExecutionStatus,
    Message,
    NodeStatus,
    Task,
    can_transition,
)
from orchestration.domain.execution import EXECUTION_TRANSITIONS, NodeState
from orchestration.errors import InvalidStateTransitionError

pytestmark = pytest.mark.unit


class TestTransitionTable:
    def test_every_status_has_an_entry(self) -> None:
        """A missing entry would silently forbid all transitions from that state."""
        assert set(EXECUTION_TRANSITIONS) == set(ExecutionStatus)

    @pytest.mark.parametrize(
        "status",
        [
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.BUDGET_EXCEEDED,
            ExecutionStatus.TIMED_OUT,
        ],
    )
    def test_terminal_statuses_have_no_exits(self, status: ExecutionStatus) -> None:
        assert status.is_terminal is True
        assert EXECUTION_TRANSITIONS[status] == frozenset()

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (ExecutionStatus.PENDING, ExecutionStatus.RUNNING),
            (ExecutionStatus.RUNNING, ExecutionStatus.WAITING_FOR_APPROVAL),
            (ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED),
            (ExecutionStatus.RUNNING, ExecutionStatus.RUNNING),
            (ExecutionStatus.WAITING_FOR_APPROVAL, ExecutionStatus.RUNNING),
            (ExecutionStatus.RUNNING, ExecutionStatus.BUDGET_EXCEEDED),
        ],
    )
    def test_permitted_moves(self, source: ExecutionStatus, target: ExecutionStatus) -> None:
        assert can_transition(source, target) is True

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (ExecutionStatus.SUCCEEDED, ExecutionStatus.RUNNING),
            (ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED),
            (ExecutionStatus.CANCELLED, ExecutionStatus.RUNNING),
            (ExecutionStatus.PENDING, ExecutionStatus.SUCCEEDED),
            (ExecutionStatus.PENDING, ExecutionStatus.WAITING_FOR_APPROVAL),
            (ExecutionStatus.WAITING_FOR_APPROVAL, ExecutionStatus.SUCCEEDED),
        ],
    )
    def test_forbidden_moves(self, source: ExecutionStatus, target: ExecutionStatus) -> None:
        assert can_transition(source, target) is False

    def test_running_is_reentrant_so_resume_can_reassert_it(self) -> None:
        """Resume re-asserts RUNNING on an execution stranded by a crash."""
        assert can_transition(ExecutionStatus.RUNNING, ExecutionStatus.RUNNING) is True

    def test_resumable_statuses(self) -> None:
        resumable = {s for s in ExecutionStatus if s.is_resumable}
        assert resumable == {
            ExecutionStatus.PENDING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_FOR_APPROVAL,
        }


class TestExecutionStateTransitions:
    def test_happy_path(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.transition_to(ExecutionStatus.SUCCEEDED)
        assert execution_state.status is ExecutionStatus.SUCCEEDED

    def test_approval_pause_and_resume(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.transition_to(ExecutionStatus.WAITING_FOR_APPROVAL)
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.transition_to(ExecutionStatus.SUCCEEDED)
        assert execution_state.status is ExecutionStatus.SUCCEEDED

    def test_illegal_transition_raises(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.transition_to(ExecutionStatus.FAILED)
        with pytest.raises(InvalidStateTransitionError) as info:
            execution_state.transition_to(ExecutionStatus.RUNNING)
        assert info.value.context["source"] == "failed"
        assert info.value.context["target"] == "running"

    def test_started_at_is_set_once(self, execution_state: ExecutionState) -> None:
        """Re-entering RUNNING after a resume must not reset the start time.

        The duration budget is derived from ``started_at``; resetting it would
        hand a resumed execution a fresh time allowance.
        """
        execution_state.transition_to(ExecutionStatus.RUNNING)
        first = execution_state.started_at
        execution_state.transition_to(ExecutionStatus.WAITING_FOR_APPROVAL)
        execution_state.transition_to(ExecutionStatus.RUNNING)
        assert execution_state.started_at == first

    def test_terminal_transition_records_completion_and_clears_current_nodes(
        self, execution_state: ExecutionState
    ) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        in_flight: tuple[str, ...] = ("a", "b")
        execution_state.current_nodes = in_flight
        execution_state.transition_to(ExecutionStatus.CANCELLED, reason="operator cancelled")
        assert execution_state.completed_at is not None
        assert len(execution_state.current_nodes) == 0
        assert execution_state.failure_reason == "operator cancelled"

    def test_elapsed_is_zero_before_start(self, execution_state: ExecutionState) -> None:
        assert execution_state.elapsed_seconds == 0.0

    def test_elapsed_freezes_after_completion(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.transition_to(ExecutionStatus.SUCCEEDED)
        first = execution_state.elapsed_seconds
        assert execution_state.elapsed_seconds == first


class TestNodeState:
    def test_attempts_accumulate_across_retries(self) -> None:
        node = NodeState(node_id="a")
        node.mark_running()
        node.mark_running()
        node.mark_running()
        assert node.attempts == 3

    def test_started_at_is_recorded_only_once(self) -> None:
        node = NodeState(node_id="a")
        node.mark_running()
        first = node.started_at
        node.mark_running()
        assert node.started_at == first

    def test_success_records_confidence_and_duration(self) -> None:
        node = NodeState(node_id="a")
        node.mark_running()
        node.mark_succeeded(confidence=0.77)
        assert node.status is NodeStatus.SUCCEEDED
        assert node.confidence == 0.77
        assert node.duration_seconds is not None
        assert node.duration_seconds >= 0.0

    def test_failure_records_the_error(self) -> None:
        node = NodeState(node_id="a")
        node.mark_running()
        node.mark_failed({"code": "timeout", "message": "slow"})
        assert node.status is NodeStatus.FAILED
        assert node.error == {"code": "timeout", "message": "slow"}

    def test_skipped_counts_as_complete_but_not_succeeded(self) -> None:
        """A conditional branch not taken must not stall a downstream join."""
        node = NodeState(node_id="a")
        node.mark_skipped("branch not taken")
        assert node.status is NodeStatus.SKIPPED
        assert node.is_complete is True
        assert node.is_terminal is True

    def test_failed_is_terminal_but_not_complete(self) -> None:
        """A failure is final for the node but does not satisfy a strict join."""
        node = NodeState(node_id="a")
        node.mark_running()
        node.mark_failed({"code": "x", "message": "y"})
        assert node.is_terminal is True
        assert node.is_complete is False

    def test_duration_stays_none_if_never_started(self) -> None:
        node = NodeState(node_id="a")
        node.mark_succeeded()
        assert node.duration_seconds is None


class TestNodeBookkeeping:
    def test_node_state_is_created_on_demand(self, execution_state: ExecutionState) -> None:
        state = execution_state.node_state("fresh")
        assert state.node_id == "fresh"
        assert execution_state.node_state("fresh") is state, "should return the same instance"

    def test_completed_includes_skipped(self, execution_state: ExecutionState) -> None:
        execution_state.node_state("a").mark_succeeded()
        execution_state.node_state("b").mark_skipped("not taken")
        execution_state.node_state("c").mark_running()
        assert execution_state.completed_node_ids() == frozenset({"a", "b"})
        assert execution_state.succeeded_node_ids() == frozenset({"a"})

    def test_failed_node_ids(self, execution_state: ExecutionState) -> None:
        execution_state.node_state("a").mark_failed({"code": "e", "message": "m"})
        assert execution_state.failed_node_ids() == frozenset({"a"})

    def test_attempts_for_unknown_node_is_zero(self, execution_state: ExecutionState) -> None:
        assert execution_state.attempts_for("nope") == 0


class TestRetryAndRecoveryAccounting:
    def test_record_retry_increments_both_counters(self, execution_state: ExecutionState) -> None:
        """Node-level retries and the budget retry tally must stay in step."""
        assert execution_state.record_retry("a") == 1
        assert execution_state.record_retry("a") == 2
        assert execution_state.retries["a"] == 2
        assert execution_state.budget_usage.retries == 2
        assert execution_state.total_retries == 2

    def test_recovery_marks_the_most_recent_matching_error(
        self, execution_state: ExecutionState
    ) -> None:
        execution_state.record_error(
            ExecutionError(node_id="a", code="timeout", message="first", retryable=True)
        )
        execution_state.record_error(
            ExecutionError(node_id="a", code="timeout", message="second", retryable=True)
        )
        execution_state.mark_last_error_recovered("a")
        assert [e.recovered for e in execution_state.errors] == [False, True]
        assert execution_state.recovered_error_count == 1

    def test_recovery_ignores_other_nodes(self, execution_state: ExecutionState) -> None:
        execution_state.record_error(ExecutionError(node_id="a", code="t", message="m"))
        execution_state.mark_last_error_recovered("b")
        assert execution_state.recovered_error_count == 0

    def test_recovery_on_empty_history_is_a_noop(self, execution_state: ExecutionState) -> None:
        execution_state.mark_last_error_recovered("a")
        assert execution_state.errors == ()

    def test_errors_are_retained_after_recovery(self, execution_state: ExecutionState) -> None:
        """A run that recovered twice is not the same as one that never failed."""
        execution_state.record_error(ExecutionError(node_id="a", code="t", message="m"))
        execution_state.mark_last_error_recovered("a")
        assert len(execution_state.errors) == 1


class TestEvaluationContext:
    def test_exposes_only_the_intended_surface(self, execution_state: ExecutionState) -> None:
        """Conditions must not be able to reach into engine internals."""
        ctx = execution_state.evaluation_context()
        assert set(ctx) == {
            "task",
            "outputs",
            "variables",
            "status",
            "retries",
            "total_retries",
            "elapsed_seconds",
            "errors",
            "node_status",
            "confidence",
        }

    def test_includes_confidence_for_scored_nodes_only(
        self, execution_state: ExecutionState
    ) -> None:
        execution_state.node_state("a").mark_succeeded(confidence=0.9)
        execution_state.node_state("b").mark_succeeded()
        ctx = execution_state.evaluation_context()
        assert ctx["confidence"] == {"a": 0.9}

    def test_reflects_variables_and_outputs(self, execution_state: ExecutionState) -> None:
        execution_state.record_agent_output("a", {"confidence": 0.5}, output_key="research")
        ctx = execution_state.evaluation_context()
        assert ctx["outputs"]["a"] == {"confidence": 0.5}
        assert ctx["variables"]["research"] == {"confidence": 0.5}

    def test_output_key_is_optional(self, execution_state: ExecutionState) -> None:
        execution_state.record_agent_output("a", {"x": 1}, output_key=None)
        assert execution_state.variables == {}
        assert execution_state.agent_outputs["a"] == {"x": 1}


class TestRecording:
    def test_add_message(self, execution_state: ExecutionState) -> None:
        execution_state.add_message(Message.user("hello"))
        assert len(execution_state.messages) == 1

    def test_add_artifact(self, execution_state: ExecutionState) -> None:
        execution_state.add_artifact(
            Artifact(name="chart.png", path="charts/chart.png", media_type="image/png")
        )
        assert execution_state.artifacts[0].name == "chart.png"

    def test_set_variable(self, execution_state: ExecutionState) -> None:
        execution_state.set_variable("k", [1, 2, 3])
        assert execution_state.variables["k"] == [1, 2, 3]


class TestSerialisationRoundTrip:
    """Resume correctness rests entirely on lossless serialisation."""

    def test_full_state_round_trips(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.node_state("a").mark_running()
        execution_state.node_state("a").mark_succeeded(confidence=0.8)
        execution_state.node_state("b").mark_skipped("branch")
        execution_state.record_error(
            ExecutionError(node_id="a", code="timeout", message="slow", retryable=True)
        )
        execution_state.mark_last_error_recovered("a")
        execution_state.record_retry("a")
        execution_state.record_agent_output("a", {"content": "x"}, output_key="res")
        execution_state.add_message(Message.assistant("done"))
        execution_state.add_artifact(Artifact(name="r.md", path="reports/r.md"))
        execution_state.budget_usage.add_llm_usage(
            input_tokens=100, output_tokens=50, cost_usd=0.001
        )

        blob = execution_state.model_dump(mode="json")
        restored = ExecutionState.model_validate(blob)

        assert restored.model_dump(mode="json") == blob

    def test_attempt_counts_survive(self, execution_state: ExecutionState) -> None:
        """A resumed node must not get a fresh retry allowance."""
        node = execution_state.node_state("a")
        node.mark_running()
        node.mark_running()
        restored = ExecutionState.model_validate(execution_state.model_dump(mode="json"))
        assert restored.node_state("a").attempts == 2

    def test_enum_values_survive(self, execution_state: ExecutionState) -> None:
        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.node_state("a").mark_skipped("x")
        restored = ExecutionState.model_validate(execution_state.model_dump(mode="json"))
        assert restored.status is ExecutionStatus.RUNNING
        assert restored.node_states["a"].status is NodeStatus.SKIPPED

    def test_json_dump_is_serialisable_to_jsonb(self, execution_state: ExecutionState) -> None:
        """Every value must be JSON-native for the JSONB column."""
        import json

        execution_state.transition_to(ExecutionStatus.RUNNING)
        execution_state.add_artifact(Artifact(name="a", path="p"))
        encoded = json.dumps(execution_state.model_dump(mode="json"))
        assert json.loads(encoded)["status"] == "running"


class TestSummary:
    def test_summary_reports_progress(self, task: Task) -> None:
        state = ExecutionState(execution_id="e1", workflow_id="w1", task=task)
        state.transition_to(ExecutionStatus.RUNNING)
        state.node_state("a").mark_succeeded()
        state.node_state("b").mark_failed({"code": "x", "message": "y"})
        state.record_retry("b")
        summary = state.summary()
        assert summary["nodes_succeeded"] == 1
        assert summary["nodes_failed"] == 1
        assert summary["total_retries"] == 1
        assert summary["status"] == "running"
