"""Checkpoint/resume integration tests against real PostgreSQL.

The claim being verified: an execution interrupted mid-flight resumes from its
last checkpoint and finishes, without redoing completed work and without a
separate recovery code path.

"Interrupted" here means genuinely abandoned -- the executor object, its event
bus, and its budget meter are all discarded, and a *fresh* set is built from
persisted state alone. Anything the engine kept only in memory is therefore gone,
which is exactly the condition resume has to survive.
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
    ExecutionStatus,
    NodeKind,
    NodeStatus,
    PolicyEffect,
)
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task, Workflow, WorkflowEdge, WorkflowNode
from orchestration.errors import InvalidStateTransitionError, NotFoundError
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.events.sinks import PostgresEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import Fault, MockProvider, MockRule, agent_output
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import (
    EventRepository,
    ExecutionRepository,
    WorkflowRepository,
)
from orchestration.policies.engine import build_default_policy_engine
from orchestration.routing.model_router import build_default_router
from orchestration.tools.registry import build_default_registry
from orchestration.workflow.executor import CancelToken, WorkflowExecutor
from orchestration.workflow.graph import WorkflowGraph

pytestmark = pytest.mark.integration


async def _no_sleep(delay: float) -> None:
    return None


def _chain(length: int = 4) -> Workflow:
    """A linear chain, so "resumed from step N" is unambiguous."""
    agents = ["research_agent", "pricing_agent", "analyst_agent", "critic_agent"]
    nodes = tuple(
        WorkflowNode(
            id=f"n{i}",
            kind=NodeKind.AGENT,
            agent_id=agents[i % len(agents)],
            output_key=f"n{i}",
        )
        for i in range(length)
    )
    edges = tuple(WorkflowEdge(source=f"n{i}", target=f"n{i + 1}") for i in range(length - 1))
    return Workflow(name="resumable-chain", nodes=nodes, edges=edges)


class Engine:
    """A disposable engine instance, so "the process died" can be simulated.

    Each instance owns its own executor, event bus and budget meter. Discarding
    one and building another is the closest a single-process test can get to a
    restart, and it is enough to prove nothing needed for resume lives only in
    memory.
    """

    def __init__(self, database: Database, provider: MockProvider, sandbox: Path) -> None:
        self.database = database
        self.provider = provider
        self.agents = build_default_agent_registry()
        self.tools = build_default_registry()
        self.policy = build_default_policy_engine(agents=self.agents, tools=self.tools)
        self.sink = InMemoryEventSink()
        self.bus = EventBus([self.sink, PostgresEventSink(database)])
        self.manager = CheckpointManager(database)
        self.sandbox = sandbox

    def executor(
        self, workflow: Workflow, state: ExecutionState, *, event_sequence: int = 0
    ) -> WorkflowExecutor:
        # A resumed bus continues the existing numbering rather than restarting
        # at zero, which would collide with the unique (execution, sequence) index.
        self.bus = EventBus(
            [self.sink, PostgresEventSink(self.database)], start_sequence=event_sequence
        )
        meter = BudgetMeter(state.budget, state.budget_usage, elapsed=lambda: state.elapsed_seconds)
        guard = BudgetGuard(meter)

        async def authorise(
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
            authoriser=authorise,
            budget_check=guard,
        )
        return WorkflowExecutor(
            graph=WorkflowGraph(workflow),
            agents=self.agents,
            tools=self.tools,
            runtime=runtime,
            events=ExecutionEventRecorder(bus=self.bus, execution_id=state.execution_id),
            meter=meter,
            checkpoint=self.manager.writer(),  # type: ignore[arg-type]
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
            task_description="resumable work",
        )


def _state(execution_id: str, workflow: Workflow) -> ExecutionState:
    return ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow.id,
        task=Task(description="resumable work"),
        budget=UNLIMITED_BUDGET,
        budget_usage=BudgetUsage(),
    )


class TestCheckpointPersistence:
    async def test_checkpoints_are_written_during_execution(
        self, database: Database, sandbox: Path
    ) -> None:
        workflow = _chain(2)
        await _seed(database, "exec_cp1", workflow)
        engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        state = _state("exec_cp1", workflow)

        result = await engine.executor(workflow, state).run(state)
        assert result.succeeded

        history = await engine.manager.history("exec_cp1")
        assert len(history) >= 4  # started, before/after each node, finalization
        assert history[0]["reason"] == "execution_started"
        sequences = [int(h["sequence"]) for h in history]
        assert sequences == sorted(sequences)

    async def test_state_and_checkpoint_agree(self, database: Database, sandbox: Path) -> None:
        """They are written in one transaction, so they cannot disagree."""
        workflow = _chain(2)
        await _seed(database, "exec_cp2", workflow)
        engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        state = _state("exec_cp2", workflow)
        await engine.executor(workflow, state).run(state)

        live_state, _ = await engine.manager.load_state("exec_cp2")
        latest = await engine.manager.latest("exec_cp2")
        assert latest is not None
        assert live_state.status is ExecutionStatus.SUCCEEDED
        assert live_state.succeeded_node_ids() == state.succeeded_node_ids()

    async def test_identical_consecutive_writes_are_deduplicated(
        self, database: Database, sandbox: Path
    ) -> None:
        """A long run's checkpoint table stays proportional to actual progress."""
        workflow = _chain(2)
        await _seed(database, "exec_cp3", workflow)
        engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        state = _state("exec_cp3", workflow)
        state.transition_to(ExecutionStatus.RUNNING)

        from orchestration.domain.enums import CheckpointReason

        first = await engine.manager.write(state, workflow, CheckpointReason.BEFORE_NODE, "n0")
        second = await engine.manager.write(state, workflow, CheckpointReason.BEFORE_NODE, "n0")
        assert first is not None
        assert second is None, "an unchanged checkpoint was written twice"


class TestResumeAfterInterruption:
    async def test_resume_completes_an_interrupted_execution(
        self, database: Database, sandbox: Path
    ) -> None:
        """The headline capability, end to end.

        The first engine fails partway through. Everything it held is discarded.
        A second engine, built from persisted state alone, finishes the run.
        """
        workflow = _chain(4)
        await _seed(database, "exec_resume", workflow)

        # First attempt: the third node fails permanently, stopping the run.
        failing = MockProvider(
            [
                MockRule(
                    name="boom",
                    match_request_key=":analyst_agent:",
                    fault=Fault("timeout", attempts=tuple(range(1, 100))),
                ),
                MockRule(name="ok", responses=(agent_output("done"),)),
            ]
        )
        first_engine = Engine(database, failing, sandbox)
        state = _state("exec_resume", workflow)
        first_result = await first_engine.executor(workflow, state).run(state)

        assert first_result.status is ExecutionStatus.FAILED
        assert state.node_states["n0"].status is NodeStatus.SUCCEEDED
        assert state.node_states["n1"].status is NodeStatus.SUCCEEDED
        assert state.node_states["n2"].status is NodeStatus.FAILED

        # The failed execution is terminal, so resume must refuse it -- proving
        # resume does not silently restart finished work.
        with pytest.raises(InvalidStateTransitionError):
            await first_engine.manager.resume("exec_resume")

    async def test_resume_from_a_pending_state_finishes_the_run(
        self, database: Database, sandbox: Path
    ) -> None:
        """Simulates a crash: state persisted mid-run, then a fresh engine."""
        workflow = _chain(4)
        await _seed(database, "exec_crash", workflow)

        state = _state("exec_crash", workflow)
        state.transition_to(ExecutionStatus.RUNNING)

        # Persist a state where the first two nodes are already done. This is
        # exactly what a crash after step 2 leaves behind.
        for node_id in ("n0", "n1"):
            node = state.node_state(node_id)
            node.mark_running()
            node.mark_succeeded(confidence=0.8)
            state.record_agent_output(
                node_id, {"content": f"{node_id} result", "confidence": 0.8}, output_key=node_id
            )
        state.budget_usage.agent_steps = 2

        crashed_engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        from orchestration.domain.enums import CheckpointReason

        await crashed_engine.manager.write(
            state, workflow, CheckpointReason.AFTER_NODE_SUCCESS, "n1"
        )

        # --- everything above is discarded; a new engine takes over ---
        provider = MockProvider([MockRule(name="r", responses=(agent_output("resumed"),))])
        fresh = Engine(database, provider, sandbox)
        context = await resume_execution(fresh.manager, "exec_crash", require_claim=False)

        assert context.restored is True
        assert context.state.succeeded_node_ids() == {"n0", "n1"}
        assert context.state.budget_usage.agent_steps == 2, (
            "budget consumption must survive resume, or the run gets a fresh allowance"
        )

        await restore_status_for_resume(context.state)
        result = await fresh.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        assert result.succeeded
        assert context.state.succeeded_node_ids() == {"n0", "n1", "n2", "n3"}
        # Only the two remaining nodes ran; the completed work was not redone.
        assert list(result.step_groups) == [("n2",), ("n3",)]

    async def test_completed_work_is_not_repeated(self, database: Database, sandbox: Path) -> None:
        """Redoing finished work would double-spend the budget."""
        workflow = _chain(3)
        await _seed(database, "exec_norepeat", workflow)

        state = _state("exec_norepeat", workflow)
        state.transition_to(ExecutionStatus.RUNNING)
        node = state.node_state("n0")
        node.mark_running()
        node.mark_succeeded(confidence=0.9)
        state.record_agent_output("n0", {"content": "already done"}, output_key="n0")

        from orchestration.domain.enums import CheckpointReason

        seed_engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        await seed_engine.manager.write(state, workflow, CheckpointReason.AFTER_NODE_SUCCESS, "n0")

        provider = MockProvider([MockRule(name="r", responses=(agent_output("new work"),))])
        fresh = Engine(database, provider, sandbox)
        context = await resume_execution(fresh.manager, "exec_norepeat", require_claim=False)
        await restore_status_for_resume(context.state)
        await fresh.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        # n0's output is the original, and the provider was never asked for it.
        assert context.state.agent_outputs["n0"]["content"] == "already done"
        assert provider.call_count == 2, f"expected 2 agent calls, got {provider.call_count}"

    async def test_event_numbering_continues_after_resume(
        self, database: Database, sandbox: Path
    ) -> None:
        """Restarting at zero would collide with the unique sequence index."""
        workflow = _chain(3)
        await _seed(database, "exec_events", workflow)

        state = _state("exec_events", workflow)
        state.transition_to(ExecutionStatus.RUNNING)
        node = state.node_state("n0")
        node.mark_running()
        node.mark_succeeded()
        state.record_agent_output("n0", {"content": "x"}, output_key="n0")

        from orchestration.domain.enums import CheckpointReason

        first = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        await first.manager.write(state, workflow, CheckpointReason.AFTER_NODE_SUCCESS, "n0")
        # Emit some events so the resumed run must continue past them.
        for _ in range(3):
            await first.bus.emit(
                __import__(
                    "orchestration.domain.enums", fromlist=["EventType"]
                ).EventType.NODE_STARTED,
                execution_id="exec_events",
                message="pre-crash event",
            )

        async with database.session() as session:
            before = await EventRepository(session).max_sequence("exec_events")
        assert before == 3

        fresh = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("y"),))]), sandbox
        )
        context = await resume_execution(fresh.manager, "exec_events", require_claim=False)
        await restore_status_for_resume(context.state)
        await fresh.executor(
            context.workflow, context.state, event_sequence=context.event_sequence
        ).run(context.state)

        async with database.session() as session:
            events = await EventRepository(session).query(
                "exec_events",
                __import__("orchestration.domain.events", fromlist=["EventFilter"]).EventFilter(
                    limit=500
                ),
            )
        sequences = [e.sequence for e in events]
        assert len(sequences) == len(set(sequences)), "duplicate event sequence after resume"
        assert max(sequences) > before

    async def test_resume_of_an_unknown_execution_raises(
        self, database: Database, sandbox: Path
    ) -> None:
        engine = Engine(database, MockProvider(), sandbox)
        with pytest.raises(NotFoundError):
            await engine.manager.resume("exec_does_not_exist")

    async def test_resume_of_a_finished_execution_is_refused(
        self, database: Database, sandbox: Path
    ) -> None:
        """Resuming a completed run would redo side effects."""
        workflow = _chain(2)
        await _seed(database, "exec_done", workflow)
        engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        state = _state("exec_done", workflow)
        await engine.executor(workflow, state).run(state)

        with pytest.raises(InvalidStateTransitionError):
            await engine.manager.resume("exec_done")


class TestResumeClaiming:
    async def test_stranded_executions_are_discoverable(
        self, database: Database, sandbox: Path
    ) -> None:
        """After a restart a worker must be able to find abandoned work."""
        workflow = _chain(2)
        await _seed(database, "exec_stranded", workflow)
        async with database.session() as session:
            await ExecutionRepository(session).update_status(
                "exec_stranded", ExecutionStatus.RUNNING
            )

        engine = Engine(database, MockProvider(), sandbox)
        resumable = await engine.manager.find_resumable()
        assert "exec_stranded" in resumable

    async def test_finished_executions_are_not_offered_for_resume(
        self, database: Database, sandbox: Path
    ) -> None:
        workflow = _chain(2)
        await _seed(database, "exec_finished", workflow)
        async with database.session() as session:
            await ExecutionRepository(session).update_status(
                "exec_finished", ExecutionStatus.SUCCEEDED
            )
        engine = Engine(database, MockProvider(), sandbox)
        assert "exec_finished" not in await engine.manager.find_resumable()

    async def test_claim_is_exclusive_within_a_transaction(
        self, database: Database, sandbox: Path
    ) -> None:
        """Two workers finding the same stranded run: only one proceeds."""
        workflow = _chain(2)
        await _seed(database, "exec_claim", workflow)
        from orchestration.persistence.repositories import ExecutionStateRepository

        async with database.session() as first:
            assert await ExecutionStateRepository(first).acquire_advisory_lock("exec_claim")
            async with database.session() as second:
                assert (
                    await ExecutionStateRepository(second).acquire_advisory_lock("exec_claim")
                    is False
                )


class TestRewind:
    async def test_resume_from_a_specific_checkpoint(
        self, database: Database, sandbox: Path
    ) -> None:
        """The operator escape hatch when the newest checkpoint is the problem."""
        workflow = _chain(3)
        await _seed(database, "exec_rewind", workflow)
        engine = Engine(
            database, MockProvider([MockRule(name="r", responses=(agent_output("x"),))]), sandbox
        )
        state = _state("exec_rewind", workflow)
        await engine.executor(workflow, state).run(state)

        history = await engine.manager.history("exec_rewind")
        early = next(h for h in history if h["reason"] == "before_node")
        early_sequence = int(early["sequence"])
        context = await engine.manager.resume_from_sequence("exec_rewind", early_sequence)
        assert context.checkpoint.sequence == early_sequence
        # The rewound state is genuinely earlier than the finished one.
        assert len(context.state.succeeded_node_ids()) < 3

    async def test_rewind_to_a_missing_sequence_raises(
        self, database: Database, sandbox: Path
    ) -> None:
        workflow = _chain(2)
        await _seed(database, "exec_rewind2", workflow)
        engine = Engine(database, MockProvider(), sandbox)
        with pytest.raises(NotFoundError):
            await engine.manager.resume_from_sequence("exec_rewind2", 999)
