"""Integration tests for dynamic, supervisor-driven execution.

:class:`ExecutionOrchestrator` has no static workflow to validate against --
the whole point is that the supervisor builds the graph as it goes. What has
to be verified here, against a real database, is that the compiled graph is
still a real, checkpointed, resumable execution: delegation produces agent
nodes that actually run, a requested approval pauses durably and survives a
process restart exactly like a static approval node does, and a run that
never concludes is bounded rather than looping forever.
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
from orchestration.domain.enums import ExecutionStatus, NodeStatus, PolicyEffect
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.events.sinks import PostgresEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider, MockRule, agent_output, routing_decision
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import ExecutionRepository, WorkflowRepository
from orchestration.policies.approvals import ApprovalService
from orchestration.policies.engine import build_default_policy_engine
from orchestration.routing.model_router import build_default_router
from orchestration.runtime.orchestrator import ExecutionOrchestrator, seed_dynamic_workflow
from orchestration.supervisor.supervisor import Supervisor
from orchestration.tools.registry import build_default_registry

pytestmark = pytest.mark.integration


async def _no_sleep(delay: float) -> None:
    return None


class Engine:
    """A disposable dynamic-execution engine, so a process restart can be simulated."""

    def __init__(self, database: Database, provider: MockProvider, sandbox: Path) -> None:
        self.database = database
        self.provider = provider
        self.agents = build_default_agent_registry()
        self.tools = build_default_registry()
        self.policy = build_default_policy_engine(agents=self.agents, tools=self.tools)
        self.sink = InMemoryEventSink()
        self.manager = CheckpointManager(database)
        self.sandbox = sandbox

    def orchestrator(
        self, state: ExecutionState, *, event_sequence: int = 0, max_turns: int = 40
    ) -> ExecutionOrchestrator:
        bus = EventBus([self.sink, PostgresEventSink(self.database)], start_sequence=event_sequence)
        approvals = ApprovalService(self.database, events=bus)
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
            authoriser=approvals.tool_authoriser(  # type: ignore[arg-type]
                base_authorise, execution_id=state.execution_id
            ),
            budget_check=BudgetGuard(meter),
        )
        supervisor = Supervisor(
            agents=self.agents,
            llm=LLMClient.mock(self.provider, sleep=_no_sleep),
            router=build_default_router(),
            tools=self.tools,
        )
        return ExecutionOrchestrator(
            supervisor=supervisor,
            agents=self.agents,
            tools=self.tools,
            runtime=runtime,
            approvals=approvals,
            meter=meter,
            events=ExecutionEventRecorder(bus=bus, execution_id=state.execution_id),
            checkpoint=self.manager.writer(),  # type: ignore[arg-type]
            sandbox_root=self.sandbox,
            max_turns=max_turns,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


async def _seed(database: Database, execution_id: str, workflow_id: str) -> None:
    workflow = seed_dynamic_workflow().model_copy(update={"id": workflow_id})
    async with database.session() as session:
        await WorkflowRepository(session).save(workflow)
        await ExecutionRepository(session).create(
            execution_id=execution_id,
            workflow_id=workflow_id,
            task_description="dynamic run",
        )


def _state(execution_id: str, workflow_id: str) -> ExecutionState:
    return ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow_id,
        task=Task(description="find the price per seat of the top CRM vendors"),
        budget=UNLIMITED_BUDGET,
        budget_usage=BudgetUsage(),
    )


class TestDelegationAndFinalize:
    async def test_a_single_delegation_then_finalize_succeeds(
        self, database: Database, sandbox: Path
    ) -> None:
        await _seed(database, "exec_dyn_1", "wkf_dyn_1")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("delegate", agents=["research_agent"]),
                        routing_decision("finalize", answer="five vendors found"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("five vendors found"),)),
            ]
        )
        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_1", "wkf_dyn_1")

        result = await engine.orchestrator(state).run(state)

        assert result.succeeded
        assert result.state.final_output == "five vendors found"
        assert result.state.node_states["research_agent"].status is NodeStatus.SUCCEEDED

    async def test_parallel_delegation_runs_every_target(
        self, database: Database, sandbox: Path
    ) -> None:
        await _seed(database, "exec_dyn_2", "wkf_dyn_2")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "parallel_delegate", agents=["research_agent", "pricing_agent"]
                        ),
                        routing_decision("finalize", answer="combined report"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("partial finding"),)),
            ]
        )
        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_2", "wkf_dyn_2")

        result = await engine.orchestrator(state).run(state)

        assert result.succeeded
        assert result.state.node_states["research_agent"].status is NodeStatus.SUCCEEDED
        assert result.state.node_states["pricing_agent"].status is NodeStatus.SUCCEEDED

    async def test_a_second_delegation_round_attaches_after_the_first(
        self, database: Database, sandbox: Path
    ) -> None:
        """Two supervisor turns, two rounds of delegation, then finalize."""
        await _seed(database, "exec_dyn_3", "wkf_dyn_3")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("delegate", agents=["research_agent"]),
                        routing_decision("delegate", agents=["analyst_agent"]),
                        routing_decision("finalize", answer="done"),
                    ),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("work"),)),
            ]
        )
        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_3", "wkf_dyn_3")

        result = await engine.orchestrator(state).run(state)

        assert result.succeeded
        assert result.state.node_states["research_agent"].status is NodeStatus.SUCCEEDED
        assert result.state.node_states["analyst_agent"].status is NodeStatus.SUCCEEDED
        assert result.turns == 3


class TestFailure:
    async def test_a_fail_decision_fails_the_execution(
        self, database: Database, sandbox: Path
    ) -> None:
        await _seed(database, "exec_dyn_fail", "wkf_dyn_fail")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision("fail", failure_reason="nothing matches this request"),
                    ),
                    priority=10,
                )
            ]
        )
        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_fail", "wkf_dyn_fail")

        result = await engine.orchestrator(state).run(state)

        assert result.status is ExecutionStatus.FAILED


class TestTurnLimit:
    async def test_a_run_that_never_concludes_is_bounded(
        self, database: Database, sandbox: Path
    ) -> None:
        """A supervisor that keeps delegating forever must not loop forever."""
        await _seed(database, "exec_dyn_loop", "wkf_dyn_loop")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(routing_decision("delegate", agents=["research_agent"]),),
                    priority=10,
                ),
                MockRule(name="agent", responses=(agent_output("work"),)),
            ]
        )
        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_loop", "wkf_dyn_loop")

        result = await engine.orchestrator(state, max_turns=2).run(state)

        assert result.status is ExecutionStatus.FAILED
        assert result.turns == 2
        assert "supervisor turns" in (result.state.failure_reason or "")


class TestHumanApproval:
    async def test_a_requested_approval_pauses_durably_and_resumes(
        self, database: Database, sandbox: Path
    ) -> None:
        """The headline claim: a supervisor-requested approval survives a restart."""
        await _seed(database, "exec_dyn_appr", "wkf_dyn_appr")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="publish the report externally",
                            approval_risk_reason="this is visible to customers",
                        ),
                        routing_decision("finalize", answer="published"),
                    ),
                    priority=10,
                )
            ]
        )

        first = Engine(database, provider, sandbox)
        state = _state("exec_dyn_appr", "wkf_dyn_appr")
        paused = await first.orchestrator(state).run(state)

        assert paused.is_paused
        assert state.status is ExecutionStatus.WAITING_FOR_APPROVAL
        approval_id = state.pending_approval_id
        assert approval_id is not None

        reviewer = ApprovalService(database)
        await reviewer.approve(approval_id, by="ops@example.test", note="checked")

        second = Engine(database, provider, sandbox)
        context = await resume_execution(second.manager, "exec_dyn_appr", require_claim=False)
        await restore_status_for_resume(context.state)

        result = await second.orchestrator(
            context.state, event_sequence=context.event_sequence
        ).run(context.state, context.workflow)

        assert result.succeeded
        assert result.state.pending_approval_id is None

    async def test_a_rejected_approval_fails_the_execution(
        self, database: Database, sandbox: Path
    ) -> None:
        await _seed(database, "exec_dyn_reject", "wkf_dyn_reject")
        provider = MockProvider(
            [
                MockRule(
                    name="supervisor",
                    match_system="supervisor",
                    responses=(
                        routing_decision(
                            "request_human_approval",
                            approval_action="delete the staging dataset",
                            approval_risk_reason="destructive and irreversible",
                        ),
                    ),
                    priority=10,
                )
            ]
        )

        engine = Engine(database, provider, sandbox)
        state = _state("exec_dyn_reject", "wkf_dyn_reject")
        await engine.orchestrator(state).run(state)
        approval_id = state.pending_approval_id
        assert approval_id is not None

        reviewer = ApprovalService(database)
        await reviewer.reject(approval_id, by="ops@example.test", note="too risky")

        second = Engine(database, provider, sandbox)
        context = await resume_execution(second.manager, "exec_dyn_reject", require_claim=False)
        await restore_status_for_resume(context.state)
        result = await second.orchestrator(context.state).run(context.state, context.workflow)

        assert result.status is ExecutionStatus.FAILED
