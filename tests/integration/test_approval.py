"""Human-in-the-loop integration tests against real PostgreSQL.

The claim being verified: an execution that reaches an approval gate pauses
*durably*, survives the loss of the process that paused it, and continues after a
human decides -- without the gate re-pausing forever and without any action
executing before it was authorised.

Every test discards the engine that paused and builds a fresh one to resume,
because a pause that only works while the original coroutine is alive is not a
durable pause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestration.agents.definitions import build_default_agent_registry
from orchestration.agents.runtime import AgentRuntime
from orchestration.budget.meter import BudgetGuard, BudgetMeter
from orchestration.checkpoint.manager import (
    CheckpointManager,
    restore_status_for_resume,
    resume_execution,
)
from orchestration.domain.base import JsonDict
from orchestration.domain.budget import UNLIMITED_BUDGET, BudgetUsage
from orchestration.domain.enums import (
    ApprovalStatus,
    CheckpointReason,
    EventType,
    ExecutionStatus,
    NodeKind,
    NodeStatus,
    PolicyEffect,
)
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task, Workflow, WorkflowEdge, WorkflowNode
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.events.sinks import PostgresEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider, MockRule, agent_output
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import ExecutionRepository, WorkflowRepository
from orchestration.policies.approvals import ApprovalService, approval_key
from orchestration.policies.engine import build_default_policy_engine
from orchestration.routing.model_router import build_default_router
from orchestration.tools.registry import build_default_registry
from orchestration.workflow.executor import CancelToken, WorkflowExecutor
from orchestration.workflow.graph import WorkflowGraph

pytestmark = pytest.mark.integration


async def _no_sleep(delay: float) -> None:
    return None


def _gated_workflow() -> Workflow:
    """research -> approval gate -> finalize."""
    return Workflow(
        name="gated",
        nodes=(
            WorkflowNode(
                id="research", kind=NodeKind.AGENT, agent_id="research_agent", output_key="research"
            ),
            WorkflowNode(
                id="gate",
                kind=NodeKind.APPROVAL,
                approval_reason="publishing the report is externally visible",
            ),
            WorkflowNode(
                id="publish", kind=NodeKind.AGENT, agent_id="finalizer_agent", output_key="publish"
            ),
        ),
        edges=(
            WorkflowEdge(source="research", target="gate"),
            WorkflowEdge(source="gate", target="publish"),
        ),
    )


class Engine:
    """A disposable engine, so losing the process can be simulated."""

    def __init__(self, database: Database, provider: MockProvider, sandbox: Path) -> None:
        self.database = database
        self.provider = provider
        self.agents = build_default_agent_registry()
        self.tools = build_default_registry()
        self.policy = build_default_policy_engine(agents=self.agents, tools=self.tools)
        self.sink = InMemoryEventSink()
        self.bus = EventBus([self.sink, PostgresEventSink(database)])
        self.manager = CheckpointManager(database)
        self.approvals = ApprovalService(database, events=self.bus)
        self.sandbox = sandbox

    def executor(
        self, workflow: Workflow, state: ExecutionState, *, event_sequence: int = 0
    ) -> WorkflowExecutor:
        self.bus = EventBus(
            [self.sink, PostgresEventSink(self.database)], start_sequence=event_sequence
        )
        self.approvals = ApprovalService(self.database, events=self.bus)
        meter = BudgetMeter(state.budget, state.budget_usage, elapsed=lambda: state.elapsed_seconds)

        async def base_authorise(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            decision = self.policy.evaluate(agent_id, tool, arguments)
            if decision.allowed:
                self.policy.record_call(agent_id, tool)
            return decision.effect, decision.reason

        runtime = AgentRuntime(
            llm=LLMClient.mock(self.provider, sleep=_no_sleep),
            tools=self.tools,
            router=build_default_router(),
            # The wrapper is what stops a granted approval being re-requested.
            authoriser=self.approvals.tool_authoriser(  # type: ignore[arg-type]
                base_authorise, execution_id=state.execution_id
            ),
            budget_check=BudgetGuard(meter),
        )
        return WorkflowExecutor(
            graph=WorkflowGraph(workflow),
            agents=self.agents,
            tools=self.tools,
            runtime=runtime,
            events=ExecutionEventRecorder(bus=self.bus, execution_id=state.execution_id),
            meter=meter,
            checkpoint=self.manager.writer(),  # type: ignore[arg-type]
            approval_gate=self.approvals.gate(),  # type: ignore[arg-type]
            cancel_token=CancelToken(),
            sandbox_root=self.sandbox,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


async def _seed(database: Database, execution_id: str, workflow: Workflow) -> None:
    async with database.session() as session:
        await WorkflowRepository(session).save(workflow)
        await ExecutionRepository(session).create(
            execution_id=execution_id,
            workflow_id=workflow.id,
            task_description="publish a report",
        )


def _state(execution_id: str, workflow: Workflow) -> ExecutionState:
    return ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow.id,
        task=Task(description="publish a report"),
        budget=UNLIMITED_BUDGET,
        budget_usage=BudgetUsage(),
    )


def _provider() -> MockProvider:
    return MockProvider([MockRule(name="any", responses=(agent_output("work done"),))])


class TestApprovalKey:
    """The identity that makes a pause survivable."""

    def test_same_action_yields_the_same_key(self) -> None:
        a = approval_key(
            execution_id="e1", action="tool:send_email", node_id="n1", arguments={"to": "x"}
        )
        b = approval_key(
            execution_id="e1", action="tool:send_email", node_id="n1", arguments={"to": "x"}
        )
        assert a == b

    def test_argument_order_does_not_change_the_key(self) -> None:
        a = approval_key(execution_id="e1", action="a", arguments={"x": 1, "y": 2})
        b = approval_key(execution_id="e1", action="a", arguments={"y": 2, "x": 1})
        assert a == b

    def test_different_arguments_are_a_different_request(self) -> None:
        """Approving an email to one recipient must not authorise another."""
        a = approval_key(execution_id="e1", action="tool:send_email", arguments={"to": "a@b.test"})
        b = approval_key(execution_id="e1", action="tool:send_email", arguments={"to": "c@d.test"})
        assert a != b

    def test_different_executions_do_not_share_approvals(self) -> None:
        a = approval_key(execution_id="e1", action="a")
        b = approval_key(execution_id="e2", action="a")
        assert a != b


class TestPauseAndResume:
    async def test_execution_pauses_at_the_gate(self, database: Database, sandbox: Path) -> None:
        workflow = _gated_workflow()
        await _seed(database, "exec_gate", workflow)
        engine = Engine(database, _provider(), sandbox)
        state = _state("exec_gate", workflow)

        result = await engine.executor(workflow, state).run(state)

        assert result.is_paused
        assert state.status is ExecutionStatus.WAITING_FOR_APPROVAL
        assert state.node_states["research"].status is NodeStatus.SUCCEEDED
        assert state.node_states["gate"].status is NodeStatus.WAITING_FOR_APPROVAL
        # The gated work must not have run.
        assert "publish" not in state.node_states
        assert state.pending_approval_id is not None

    async def test_pause_is_checkpointed_and_the_approval_is_persisted(
        self, database: Database, sandbox: Path
    ) -> None:
        workflow = _gated_workflow()
        await _seed(database, "exec_gate2", workflow)
        engine = Engine(database, _provider(), sandbox)
        state = _state("exec_gate2", workflow)
        await engine.executor(workflow, state).run(state)

        reasons = [h["reason"] for h in await engine.manager.history("exec_gate2")]
        assert CheckpointReason.BEFORE_APPROVAL.value in reasons

        pending = await engine.approvals.pending_for("exec_gate2")
        assert len(pending) == 1
        assert "externally visible" in pending[0].risk_reason

    async def test_a_fresh_engine_resumes_and_finishes_after_approval(
        self, database: Database, sandbox: Path
    ) -> None:
        """The headline human-in-the-loop path, across a simulated restart."""
        workflow = _gated_workflow()
        await _seed(database, "exec_hitl", workflow)

        # --- process 1: runs until the gate, then is discarded ---
        first = Engine(database, _provider(), sandbox)
        state = _state("exec_hitl", workflow)
        paused = await first.executor(workflow, state).run(state)
        assert paused.is_paused
        approval_id = state.pending_approval_id
        assert approval_id is not None

        # --- a reviewer decides, out of band ---
        reviewer = ApprovalService(database)
        decided = await reviewer.approve(approval_id, by="ops@example.test", note="checked")
        assert decided.status is ApprovalStatus.APPROVED

        # --- process 2: knows nothing except what is in the database ---
        second = Engine(database, _provider(), sandbox)
        context = await resume_execution(second.manager, "exec_hitl", require_claim=False)
        assert context.state.status is ExecutionStatus.WAITING_FOR_APPROVAL
        await restore_status_for_resume(context.state)

        result = await second.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        assert result.succeeded
        assert context.state.node_states["gate"].status is NodeStatus.SUCCEEDED
        assert context.state.node_states["publish"].status is NodeStatus.SUCCEEDED
        assert context.state.pending_approval_id is None

    async def test_resuming_without_a_decision_pauses_again(
        self, database: Database, sandbox: Path
    ) -> None:
        """Resume must not treat an undecided gate as permission."""
        workflow = _gated_workflow()
        await _seed(database, "exec_undecided", workflow)

        first = Engine(database, _provider(), sandbox)
        state = _state("exec_undecided", workflow)
        await first.executor(workflow, state).run(state)

        second = Engine(database, _provider(), sandbox)
        context = await resume_execution(second.manager, "exec_undecided", require_claim=False)
        await restore_status_for_resume(context.state)
        result = await second.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        assert result.is_paused
        assert "publish" not in context.state.node_states

    async def test_rejection_fails_the_execution(self, database: Database, sandbox: Path) -> None:
        """A refusal is terminal: the gated work never runs and is not retried."""
        workflow = _gated_workflow()
        await _seed(database, "exec_rejected", workflow)

        first = Engine(database, _provider(), sandbox)
        state = _state("exec_rejected", workflow)
        await first.executor(workflow, state).run(state)
        approval_id = state.pending_approval_id
        assert approval_id is not None

        reviewer = ApprovalService(database)
        await reviewer.reject(approval_id, by="ops@example.test", note="not authorised")

        second = Engine(database, _provider(), sandbox)
        context = await resume_execution(second.manager, "exec_rejected", require_claim=False)
        await restore_status_for_resume(context.state)
        result = await second.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        assert result.status is ExecutionStatus.FAILED
        assert context.state.node_states["gate"].status is NodeStatus.FAILED
        # The gated work never ran. It is marked SKIPPED rather than absent
        # because a refused gate makes its whole downstream branch unreachable.
        assert context.state.node_states["publish"].status is NodeStatus.SKIPPED
        assert "publish" not in context.state.agent_outputs
        # Two attempts across the execution's whole life: one that paused, one
        # that read the rejection. Attempts accumulate rather than resetting on
        # resume, and a refusal is terminal, so there is no third.
        assert context.state.node_states["gate"].attempts == 2

    async def test_approval_events_are_recorded(self, database: Database, sandbox: Path) -> None:
        workflow = _gated_workflow()
        await _seed(database, "exec_events_appr", workflow)

        first = Engine(database, _provider(), sandbox)
        state = _state("exec_events_appr", workflow)
        await first.executor(workflow, state).run(state)
        assert first.sink.count(EventType.APPROVAL_REQUESTED) >= 1

        approval_id = state.pending_approval_id
        assert approval_id is not None
        second = Engine(database, _provider(), sandbox)
        await second.approvals.approve(approval_id, by="ops")
        context = await resume_execution(second.manager, "exec_events_appr", require_claim=False)
        await restore_status_for_resume(context.state)
        await second.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        assert second.sink.count(EventType.APPROVAL_GRANTED) >= 1


class TestDecisionIdempotency:
    async def test_approving_twice_is_safe(self, database: Database, sandbox: Path) -> None:
        """A double-clicked button must not error."""
        workflow = _gated_workflow()
        await _seed(database, "exec_double", workflow)
        engine = Engine(database, _provider(), sandbox)
        state = _state("exec_double", workflow)
        await engine.executor(workflow, state).run(state)
        approval_id = state.pending_approval_id
        assert approval_id is not None

        first = await engine.approvals.approve(approval_id, by="ops")
        second = await engine.approvals.approve(approval_id, by="ops")
        assert first.status is second.status is ApprovalStatus.APPROVED

    async def test_reversing_a_decision_is_refused(self, database: Database, sandbox: Path) -> None:
        from orchestration.errors import InvalidStateTransitionError

        workflow = _gated_workflow()
        await _seed(database, "exec_reverse", workflow)
        engine = Engine(database, _provider(), sandbox)
        state = _state("exec_reverse", workflow)
        await engine.executor(workflow, state).run(state)
        approval_id = state.pending_approval_id
        assert approval_id is not None

        await engine.approvals.approve(approval_id, by="ops")
        with pytest.raises(InvalidStateTransitionError):
            await engine.approvals.reject(approval_id, by="someone-else")


class TestToolLevelApproval:
    """A HIGH-risk tool is gated by policy, not by asking the model nicely."""

    async def test_a_granted_tool_approval_is_not_re_requested(
        self, database: Database, sandbox: Path
    ) -> None:
        """Without the authoriser wrapper this would pause in a loop."""
        service = ApprovalService(database)
        await _seed(database, "exec_tool_appr", _gated_workflow())

        async def always_require(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            return PolicyEffect.REQUIRE_APPROVAL, "sends external email"

        wrapped = service.tool_authoriser(always_require, execution_id="exec_tool_appr")

        # First encounter: undecided, so approval is still required.
        effect, _ = await wrapped("research_agent", "send_email", {"to": "a@b.test"})  # type: ignore[operator]
        assert effect is PolicyEffect.REQUIRE_APPROVAL

        outcome = await service.resolve(
            execution_id="exec_tool_appr",
            action="tool:send_email",
            risk_reason="sends external email",
            agent_id="research_agent",
            tool="send_email",
            arguments={"to": "a@b.test"},
        )
        await service.approve(outcome.request.id, by="ops")

        # Second encounter: the decision is read, and the call is allowed.
        effect, reason = await wrapped(  # type: ignore[operator]
            "research_agent", "send_email", {"to": "a@b.test"}
        )
        assert effect is PolicyEffect.ALLOW
        assert "approved by ops" in reason

    async def test_an_approval_does_not_transfer_to_different_arguments(
        self, database: Database, sandbox: Path
    ) -> None:
        """Approving an email to one address must not authorise another."""
        service = ApprovalService(database)
        await _seed(database, "exec_scope", _gated_workflow())

        async def always_require(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            return PolicyEffect.REQUIRE_APPROVAL, "sends external email"

        wrapped = service.tool_authoriser(always_require, execution_id="exec_scope")

        approved = await service.resolve(
            execution_id="exec_scope",
            action="tool:send_email",
            risk_reason="r",
            tool="send_email",
            arguments={"to": "approved@example.test"},
        )
        await service.approve(approved.request.id, by="ops")

        effect, _ = await wrapped(  # type: ignore[operator]
            "research_agent", "send_email", {"to": "someone-else@example.test"}
        )
        assert effect is PolicyEffect.REQUIRE_APPROVAL, (
            "an approval leaked to a different recipient"
        )

    async def test_a_rejected_tool_approval_denies_the_call(
        self, database: Database, sandbox: Path
    ) -> None:
        service = ApprovalService(database)
        await _seed(database, "exec_tool_deny", _gated_workflow())

        async def always_require(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            return PolicyEffect.REQUIRE_APPROVAL, "sends external email"

        wrapped = service.tool_authoriser(always_require, execution_id="exec_tool_deny")
        outcome = await service.resolve(
            execution_id="exec_tool_deny",
            action="tool:send_email",
            risk_reason="r",
            tool="send_email",
            arguments={"to": "a@b.test"},
        )
        await service.reject(outcome.request.id, by="ops", note="policy forbids this")

        effect, reason = await wrapped(  # type: ignore[operator]
            "research_agent", "send_email", {"to": "a@b.test"}
        )
        assert effect is PolicyEffect.DENY
        assert "policy forbids this" in reason

    async def test_a_non_gated_effect_passes_through_untouched(
        self, database: Database, sandbox: Path
    ) -> None:
        """The wrapper must not widen what the policy engine decided."""
        service = ApprovalService(database)

        async def deny(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
            return PolicyEffect.DENY, "not on the allowlist"

        wrapped = service.tool_authoriser(deny, execution_id="exec_passthrough")
        effect, reason = await wrapped("research_agent", "exec_shell", {})  # type: ignore[operator]
        assert effect is PolicyEffect.DENY
        assert reason == "not on the allowlist"


class TestExpiry:
    async def test_an_expired_approval_is_treated_as_a_refusal(
        self, database: Database, sandbox: Path
    ) -> None:
        """An execution parked forever is worse than one that fails cleanly."""
        service = ApprovalService(database, default_ttl_seconds=None)
        await _seed(database, "exec_expiry", _gated_workflow())

        outcome = await service.resolve(
            execution_id="exec_expiry",
            action="tool:send_email",
            risk_reason="r",
            tool="send_email",
            arguments={"to": "a@b.test"},
        )
        # Backdate the expiry so the next read sees it as overdue.
        from datetime import timedelta

        from sqlalchemy import update

        from orchestration.domain.base import utc_now
        from orchestration.persistence.tables import ApprovalRow

        async with database.session() as session:
            await session.execute(
                update(ApprovalRow)
                .where(ApprovalRow.id == outcome.request.id)
                .values(expires_at=utc_now() - timedelta(seconds=1))
            )

        again = await service.resolve(
            execution_id="exec_expiry",
            action="tool:send_email",
            risk_reason="r",
            tool="send_email",
            arguments={"to": "a@b.test"},
        )
        assert again.rejected is True
        assert again.request.status is ApprovalStatus.EXPIRED

    async def test_expire_overdue_sweeps_pending_requests(
        self, database: Database, sandbox: Path
    ) -> None:
        service = ApprovalService(database, default_ttl_seconds=None)
        await _seed(database, "exec_sweep", _gated_workflow())
        outcome = await service.resolve(
            execution_id="exec_sweep", action="a", risk_reason="r", arguments={"i": 1}
        )

        from datetime import timedelta

        from sqlalchemy import update

        from orchestration.domain.base import utc_now
        from orchestration.persistence.tables import ApprovalRow

        async with database.session() as session:
            await session.execute(
                update(ApprovalRow)
                .where(ApprovalRow.id == outcome.request.id)
                .values(expires_at=utc_now() - timedelta(seconds=1))
            )

        assert await service.expire_overdue() == 1
        assert (await service.get(outcome.request.id)).status is ApprovalStatus.EXPIRED
