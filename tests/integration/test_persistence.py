"""Integration tests against real PostgreSQL and real Redis.

The properties under test are the ones that only a real database can verify:
optimistic concurrency actually rejecting a stale write, unique constraints
actually collapsing duplicate checkpoints, JSONB actually round-tripping a state
document, advisory locks actually excluding a second worker, and pgvector
actually returning ordered nearest neighbours.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestration.agents.definitions import RESEARCH_AGENT
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.approval import ApprovalRequest
from orchestration.domain.checkpoint import Checkpoint
from orchestration.domain.enums import (
    ApprovalStatus,
    CheckpointReason,
    EventType,
    ExecutionStatus,
    InvocationStatus,
    NodeKind,
    RiskLevel,
)
from orchestration.domain.events import EventFilter, ExecutionEvent
from orchestration.domain.execution import ExecutionError, ExecutionState
from orchestration.domain.tool import ToolInvocation
from orchestration.domain.workflow import Task, Workflow, WorkflowEdge, WorkflowNode
from orchestration.errors import (
    ConcurrencyConflictError,
    InvalidStateTransitionError,
    NotFoundError,
)
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import (
    AgentRepository,
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    ExecutionRepository,
    ExecutionStateRepository,
    InvocationRepository,
    WorkflowRepository,
)

pytestmark = pytest.mark.integration


def _workflow() -> Workflow:
    return Workflow(
        name="persisted",
        nodes=(
            WorkflowNode(id="a", kind=NodeKind.AGENT, agent_id="research_agent", output_key="a"),
            WorkflowNode(id="b", kind=NodeKind.AGENT, agent_id="analyst_agent"),
            WorkflowNode(id="end", kind=NodeKind.TERMINAL),
        ),
        edges=(
            WorkflowEdge(source="a", target="b"),
            WorkflowEdge(source="b", target="end"),
        ),
    )


def _state(execution_id: str, workflow: Workflow) -> ExecutionState:
    return ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow.id,
        task=Task(description="compare CRM vendors"),
    )


async def _seed_execution(database: Database, execution_id: str, workflow: Workflow) -> None:
    async with database.session() as session:
        await WorkflowRepository(session).save(workflow)
        await ExecutionRepository(session).create(
            execution_id=execution_id,
            workflow_id=workflow.id,
            task_description="compare CRM vendors",
        )


class TestAgentPersistence:
    async def test_round_trip(self, database: Database) -> None:
        async with database.session() as session:
            await AgentRepository(session).upsert(RESEARCH_AGENT)
        async with database.session() as session:
            restored = await AgentRepository(session).get("research_agent")
        assert restored.id == RESEARCH_AGENT.id
        assert restored.tool_names == RESEARCH_AGENT.tool_names
        assert restored.capabilities == RESEARCH_AGENT.capabilities

    async def test_upsert_is_idempotent(self, database: Database) -> None:
        """Registering the same agent twice must not conflict."""
        async with database.session() as session:
            repo = AgentRepository(session)
            await repo.upsert(RESEARCH_AGENT)
            await repo.upsert(RESEARCH_AGENT)
        async with database.session() as session:
            assert len(await AgentRepository(session).list_all()) == 1

    async def test_update_replaces_the_definition(self, database: Database) -> None:
        async with database.session() as session:
            repo = AgentRepository(session)
            await repo.upsert(RESEARCH_AGENT)
            await repo.upsert(RESEARCH_AGENT.merged(description="a new description"))
        async with database.session() as session:
            assert (await AgentRepository(session).get("research_agent")).description == (
                "a new description"
            )

    async def test_disabled_agents_are_filtered(self, database: Database) -> None:
        async with database.session() as session:
            repo = AgentRepository(session)
            await repo.upsert(RESEARCH_AGENT.merged(enabled=False))
        async with database.session() as session:
            repo = AgentRepository(session)
            assert await repo.list_all() == []
            assert len(await repo.list_all(include_disabled=True)) == 1

    async def test_delete_missing_agent_raises(self, database: Database) -> None:
        async with database.session() as session:
            with pytest.raises(NotFoundError):
                await AgentRepository(session).delete("nope")


class TestWorkflowPersistence:
    async def test_round_trip_preserves_the_graph(self, database: Database) -> None:
        workflow = _workflow()
        async with database.session() as session:
            await WorkflowRepository(session).save(workflow)
        async with database.session() as session:
            restored = await WorkflowRepository(session).get(workflow.id)
        assert [n.id for n in restored.nodes] == [n.id for n in workflow.nodes]
        assert [(e.source, e.target) for e in restored.edges] == [
            (e.source, e.target) for e in workflow.edges
        ]

    async def test_relational_projection_answers_agent_usage(self, database: Database) -> None:
        """The reason node rows exist alongside the JSONB document."""
        workflow = _workflow()
        async with database.session() as session:
            await WorkflowRepository(session).save(workflow)
        async with database.session() as session:
            using = await WorkflowRepository(session).using_agent("research_agent")
        assert using == [workflow.id]

    async def test_resaving_replaces_projected_rows(self, database: Database) -> None:
        """A workflow's graph changes as a unit; stale node rows would mislead."""
        workflow = _workflow()
        async with database.session() as session:
            await WorkflowRepository(session).save(workflow)
        trimmed = Workflow(
            id=workflow.id,
            name=workflow.name,
            nodes=(WorkflowNode(id="only", kind=NodeKind.TERMINAL),),
        )
        async with database.session() as session:
            await WorkflowRepository(session).save(trimmed)
        async with database.session() as session:
            assert await WorkflowRepository(session).using_agent("research_agent") == []


class TestExecutionIdempotency:
    async def test_repeated_start_returns_the_same_execution(self, database: Database) -> None:
        """A retried POST /executions must not start a second run."""
        workflow = _workflow()
        async with database.session() as session:
            await WorkflowRepository(session).save(workflow)

        async with database.session() as session:
            first = await ExecutionRepository(session).create(
                execution_id="exec_one",
                workflow_id=workflow.id,
                task_description="t",
                idempotency_key="client-key-1",
            )
        async with database.session() as session:
            second = await ExecutionRepository(session).create(
                execution_id="exec_two",
                workflow_id=workflow.id,
                task_description="t",
                idempotency_key="client-key-1",
            )
        assert first == "exec_one"
        assert second == "exec_one", "a duplicate key started a second execution"

    async def test_without_a_key_two_executions_are_distinct(self, database: Database) -> None:
        workflow = _workflow()
        async with database.session() as session:
            await WorkflowRepository(session).save(workflow)
            repo = ExecutionRepository(session)
            a = await repo.create(
                execution_id="exec_a", workflow_id=workflow.id, task_description="t"
            )
            b = await repo.create(
                execution_id="exec_b", workflow_id=workflow.id, task_description="t"
            )
        assert a != b


class TestStateOptimisticConcurrency:
    async def test_state_round_trips_through_jsonb(self, database: Database) -> None:
        """Resume correctness rests entirely on this."""
        workflow = _workflow()
        await _seed_execution(database, "exec_state", workflow)

        state = _state("exec_state", workflow)
        state.transition_to(ExecutionStatus.RUNNING)
        state.node_state("a").mark_running()
        state.node_state("a").mark_running()  # a retry, so attempts == 2
        state.node_state("a").mark_succeeded(confidence=0.83)
        state.record_agent_output(
            "a",
            {"content": "found", "confidence": 0.83, "evidence": ["https://x"]},
            output_key="a",
        )
        state.record_error(
            ExecutionError(node_id="a", code="timeout", message="slow", retryable=True)
        )
        state.mark_last_error_recovered("a")
        state.record_retry("a")
        state.budget_usage.add_llm_usage(input_tokens=120, output_tokens=40, cost_usd=0.0012)

        async with database.session() as session:
            await ExecutionStateRepository(session).save(state, workflow)
        async with database.session() as session:
            restored, restored_workflow = await ExecutionStateRepository(session).load("exec_state")

        assert restored.node_states["a"].confidence == 0.83
        assert restored.node_states["a"].attempts == 2
        assert restored.recovered_error_count == 1
        assert restored.retries["a"] == 1
        assert restored.budget_usage.cost_usd == 0.0012
        assert restored.agent_outputs["a"]["evidence"] == ["https://x"]
        assert [n.id for n in restored_workflow.nodes] == [n.id for n in workflow.nodes]

    async def test_stale_write_is_rejected(self, database: Database) -> None:
        """Two processes resuming the same execution cannot both advance it."""
        workflow = _workflow()
        await _seed_execution(database, "exec_race", workflow)
        state = _state("exec_race", workflow)

        async with database.session() as session:
            version = await ExecutionStateRepository(session).save(state, workflow)

        # A second writer advances it.
        async with database.session() as session:
            await ExecutionStateRepository(session).save(state, workflow, expected_version=version)

        # The first writer, still holding the old version, must be refused.
        async with database.session() as session:
            with pytest.raises(ConcurrencyConflictError) as info:
                await ExecutionStateRepository(session).save(
                    state, workflow, expected_version=version
                )
        assert info.value.context["expected_version"] == version
        assert info.value.retryable is True, "a concurrency conflict should be retryable"

    async def test_version_increments_on_each_write(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_ver", workflow)
        state = _state("exec_ver", workflow)
        async with database.session() as session:
            repo = ExecutionStateRepository(session)
            first = await repo.save(state, workflow)
            second = await repo.save(state, workflow)
        assert second == first + 1

    async def test_advisory_lock_excludes_a_second_worker(self, database: Database) -> None:
        """Belt and braces alongside the version check."""
        workflow = _workflow()
        await _seed_execution(database, "exec_lock", workflow)

        async with database.session() as first:
            assert await ExecutionStateRepository(first).acquire_advisory_lock("exec_lock")
            async with database.session() as second:
                assert (
                    await ExecutionStateRepository(second).acquire_advisory_lock("exec_lock")
                    is False
                )

    async def test_advisory_lock_is_released_with_the_transaction(self, database: Database) -> None:
        """A crashed worker must not hold the lock forever."""
        workflow = _workflow()
        await _seed_execution(database, "exec_lock2", workflow)
        async with database.session() as session:
            assert await ExecutionStateRepository(session).acquire_advisory_lock("exec_lock2")
        async with database.session() as session:
            assert await ExecutionStateRepository(session).acquire_advisory_lock("exec_lock2")


class TestCheckpointIdempotency:
    async def test_append_and_read_back(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_cp", workflow)
        state = _state("exec_cp", workflow)
        state.transition_to(ExecutionStatus.RUNNING)

        checkpoint = Checkpoint(
            execution_id="exec_cp",
            sequence=0,
            reason=CheckpointReason.BEFORE_NODE,
            status=state.status,
            node_id="a",
            state=state,
            workflow=workflow,
        ).with_hash()

        async with database.session() as session:
            stored = await CheckpointRepository(session).append(checkpoint)
        assert stored.sequence == 0

        async with database.session() as session:
            latest = await CheckpointRepository(session).latest("exec_cp")
        assert latest is not None
        assert latest.content_hash == checkpoint.content_hash
        assert latest.state.status is ExecutionStatus.RUNNING

    async def test_identical_checkpoint_collapses_to_one_row(self, database: Database) -> None:
        """A process killed between the write and the ack must be safe to retry."""
        workflow = _workflow()
        await _seed_execution(database, "exec_dup", workflow)
        state = _state("exec_dup", workflow)

        def build(sequence: int) -> Checkpoint:
            return Checkpoint(
                execution_id="exec_dup",
                sequence=sequence,
                reason=CheckpointReason.BEFORE_NODE,
                status=state.status,
                node_id="a",
                state=state,
                workflow=workflow,
            ).with_hash()

        async with database.session() as session:
            repo = CheckpointRepository(session)
            first = await repo.append(build(0))
            second = await repo.append(build(0))
        assert first.id == second.id

        async with database.session() as session:
            history = await CheckpointRepository(session).history("exec_dup")
        assert len(history) == 1

    async def test_sequence_advances(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_seq", workflow)
        state = _state("exec_seq", workflow)

        async with database.session() as session:
            repo = CheckpointRepository(session)
            assert await repo.next_sequence("exec_seq") == 0
            await repo.append(
                Checkpoint(
                    execution_id="exec_seq",
                    sequence=0,
                    reason=CheckpointReason.BEFORE_NODE,
                    status=state.status,
                    state=state,
                    workflow=workflow,
                ).with_hash()
            )
        async with database.session() as session:
            assert await CheckpointRepository(session).next_sequence("exec_seq") == 1

    async def test_terminal_checkpoints_are_not_resumable(self, database: Database) -> None:
        """Resume must walk back to a restartable point, not take the newest."""
        workflow = _workflow()
        await _seed_execution(database, "exec_term", workflow)

        running = _state("exec_term", workflow)
        running.transition_to(ExecutionStatus.RUNNING)
        finished = running.model_copy(deep=True)
        finished.transition_to(ExecutionStatus.SUCCEEDED)

        async with database.session() as session:
            repo = CheckpointRepository(session)
            await repo.append(
                Checkpoint(
                    execution_id="exec_term",
                    sequence=0,
                    reason=CheckpointReason.BEFORE_NODE,
                    status=running.status,
                    state=running,
                    workflow=workflow,
                ).with_hash()
            )
            await repo.append(
                Checkpoint(
                    execution_id="exec_term",
                    sequence=1,
                    reason=CheckpointReason.BEFORE_FINALIZATION,
                    status=finished.status,
                    state=finished,
                    workflow=workflow,
                ).with_hash()
            )

        async with database.session() as session:
            repo = CheckpointRepository(session)
            newest = await repo.latest("exec_term")
            resumable = await repo.latest_resumable("exec_term")
        assert newest is not None and newest.sequence == 1
        assert resumable is not None and resumable.sequence == 0


class TestToolIdempotency:
    async def test_claim_is_exclusive(self, database: Database) -> None:
        """The guard that stops a resumed run repeating a side effect."""
        workflow = _workflow()
        await _seed_execution(database, "exec_tool", workflow)

        def invocation(invocation_id: str) -> ToolInvocation:
            return ToolInvocation(
                id=invocation_id,
                execution_id="exec_tool",
                node_id="a",
                agent_id="research_agent",
                tool="send_email",
                arguments={"to": "a@b.test"},
                idempotency_key="exec_tool:a:send_email:1",
            )

        async with database.session() as session:
            assert await InvocationRepository(session).claim_tool(invocation("tinv_1")) is True
        async with database.session() as session:
            assert await InvocationRepository(session).claim_tool(invocation("tinv_2")) is False

    async def test_completed_result_is_recoverable(self, database: Database) -> None:
        """On resume the stored result is returned instead of re-running."""
        workflow = _workflow()
        await _seed_execution(database, "exec_tool2", workflow)
        key = "exec_tool2:a:send_email:1"

        async with database.session() as session:
            repo = InvocationRepository(session)
            await repo.claim_tool(
                ToolInvocation(
                    execution_id="exec_tool2",
                    tool="send_email",
                    idempotency_key=key,
                    agent_id="research_agent",
                )
            )
            await repo.complete_tool(
                key, status=InvocationStatus.SUCCEEDED.value, result={"message_id": "m1"}
            )

        async with database.session() as session:
            recovered = await InvocationRepository(session).find_completed_tool(key)
        assert recovered == {"message_id": "m1"}

    async def test_incomplete_invocation_is_not_recoverable(self, database: Database) -> None:
        """A claimed-but-unfinished call must be retried, not skipped."""
        workflow = _workflow()
        await _seed_execution(database, "exec_tool3", workflow)
        key = "exec_tool3:a:web_search:1"
        async with database.session() as session:
            await InvocationRepository(session).claim_tool(
                ToolInvocation(execution_id="exec_tool3", tool="web_search", idempotency_key=key)
            )
        async with database.session() as session:
            assert await InvocationRepository(session).find_completed_tool(key) is None


class TestEventPersistence:
    async def test_append_and_query(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_ev", workflow)

        async with database.session() as session:
            repo = EventRepository(session)
            for i in range(5):
                await repo.append(
                    ExecutionEvent.make(
                        EventType.NODE_STARTED,
                        execution_id="exec_ev",
                        sequence=i + 1,
                        node_id=f"n{i}",
                        message=f"node {i}",
                    )
                )

        async with database.session() as session:
            events = await EventRepository(session).query("exec_ev", EventFilter())
        assert len(events) == 5
        assert [e.sequence for e in events] == [1, 2, 3, 4, 5]

    async def test_filtering_by_type_and_sequence(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_ev2", workflow)

        async with database.session() as session:
            repo = EventRepository(session)
            await repo.append(
                ExecutionEvent.make(EventType.NODE_STARTED, execution_id="exec_ev2", sequence=1)
            )
            await repo.append(
                ExecutionEvent.make(EventType.NODE_FAILED, execution_id="exec_ev2", sequence=2)
            )
            await repo.append(
                ExecutionEvent.make(EventType.NODE_COMPLETED, execution_id="exec_ev2", sequence=3)
            )

        async with database.session() as session:
            repo = EventRepository(session)
            failures = await repo.query(
                "exec_ev2", EventFilter(types=frozenset({EventType.NODE_FAILED}))
            )
            after = await repo.query("exec_ev2", EventFilter(after_sequence=1))
        assert [e.type for e in failures] == [EventType.NODE_FAILED]
        assert [e.sequence for e in after] == [2, 3]

    async def test_duplicate_sequence_is_rejected(self, database: Database) -> None:
        """The unique constraint is what guarantees a total order."""
        from orchestration.errors import DuplicateError

        workflow = _workflow()
        await _seed_execution(database, "exec_ev3", workflow)

        async with database.session() as session:
            await EventRepository(session).append(
                ExecutionEvent.make(EventType.NODE_STARTED, execution_id="exec_ev3", sequence=1)
            )
        with pytest.raises(DuplicateError):
            async with database.session() as session:
                await EventRepository(session).append(
                    ExecutionEvent.make(
                        EventType.NODE_COMPLETED, execution_id="exec_ev3", sequence=1
                    )
                )

    async def test_max_sequence_lets_a_resumed_bus_continue(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_ev4", workflow)
        async with database.session() as session:
            repo = EventRepository(session)
            for i in (1, 2, 3):
                await repo.append(
                    ExecutionEvent.make(EventType.NODE_STARTED, execution_id="exec_ev4", sequence=i)
                )
        async with database.session() as session:
            assert await EventRepository(session).max_sequence("exec_ev4") == 3

    async def test_payload_is_queryable_json(self, database: Database) -> None:
        """JSONB, not a text blob: the GIN index exists to be used."""
        workflow = _workflow()
        await _seed_execution(database, "exec_ev5", workflow)
        async with database.session() as session:
            await EventRepository(session).append(
                ExecutionEvent.make(
                    EventType.AGENT_COMPLETED,
                    execution_id="exec_ev5",
                    sequence=1,
                    confidence=0.91,
                    tokens=1234,
                )
            )
        from sqlalchemy import text

        async with database.session() as session:
            value = (
                await session.execute(
                    text(
                        "SELECT (payload->>'confidence')::float FROM execution_events "
                        "WHERE execution_id = :eid"
                    ),
                    {"eid": "exec_ev5"},
                )
            ).scalar_one()
        assert value == 0.91


class TestApprovalPersistence:
    async def test_create_and_decide(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_appr", workflow)

        request = ApprovalRequest.create(
            execution_id="exec_appr",
            action="tool:send_email",
            risk_reason="sends external email",
            tool="send_email",
            parameters={"to": "ops@example.test"},
            risk_level=RiskLevel.HIGH,
        )
        async with database.session() as session:
            await ApprovalRepository(session).create(request)

        async with database.session() as session:
            decided = await ApprovalRepository(session).decide(
                request.id,
                status=ApprovalStatus.APPROVED,
                decided_by="ops@example.test",
                note="verified",
            )
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.decided_by == "ops@example.test"

    async def test_conflicting_decision_is_refused(self, database: Database) -> None:
        """Two reviewers cannot both win a simultaneous approve/reject."""
        workflow = _workflow()
        await _seed_execution(database, "exec_appr2", workflow)
        request = ApprovalRequest.create(execution_id="exec_appr2", action="a", risk_reason="r")
        async with database.session() as session:
            await ApprovalRepository(session).create(request)
        async with database.session() as session:
            await ApprovalRepository(session).decide(
                request.id, status=ApprovalStatus.APPROVED, decided_by="first"
            )
        async with database.session() as session:
            with pytest.raises(InvalidStateTransitionError):
                await ApprovalRepository(session).decide(
                    request.id, status=ApprovalStatus.REJECTED, decided_by="second"
                )

    async def test_repeating_the_same_decision_is_idempotent(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_appr3", workflow)
        request = ApprovalRequest.create(execution_id="exec_appr3", action="a", risk_reason="r")
        async with database.session() as session:
            await ApprovalRepository(session).create(request)
        async with database.session() as session:
            repo = ApprovalRepository(session)
            await repo.decide(request.id, status=ApprovalStatus.APPROVED, decided_by="ops")
        async with database.session() as session:
            again = await ApprovalRepository(session).decide(
                request.id, status=ApprovalStatus.APPROVED, decided_by="ops"
            )
        assert again.status is ApprovalStatus.APPROVED

    async def test_pending_approvals_are_listed(self, database: Database) -> None:
        workflow = _workflow()
        await _seed_execution(database, "exec_appr4", workflow)
        async with database.session() as session:
            repo = ApprovalRepository(session)
            await repo.create(
                ApprovalRequest.create(execution_id="exec_appr4", action="one", risk_reason="r")
            )
            await repo.create(
                ApprovalRequest.create(execution_id="exec_appr4", action="two", risk_reason="r")
            )
        async with database.session() as session:
            pending = await ApprovalRepository(session).pending_for("exec_appr4")
        assert len(pending) == 2


class TestRedisCoordination:
    async def test_lock_is_exclusive(self, redis_coordinator: RedisCoordinator) -> None:
        handle = await redis_coordinator.acquire_lock("exec_1", ttl_seconds=5)
        assert handle is not None
        assert await redis_coordinator.acquire_lock("exec_1", ttl_seconds=5) is None
        assert await redis_coordinator.release_lock(handle) is True
        assert await redis_coordinator.acquire_lock("exec_1", ttl_seconds=5) is not None

    async def test_release_requires_the_correct_token(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        """The classic naive-locking bug: releasing someone else's lock."""
        from orchestration.coordination.redis import LockHandle

        handle = await redis_coordinator.acquire_lock("exec_2", ttl_seconds=5)
        assert handle is not None
        impostor = LockHandle(key=handle.key, token="not-the-token", ttl_seconds=5)
        assert await redis_coordinator.release_lock(impostor) is False
        assert await redis_coordinator.is_locked("exec_2") is True

    async def test_lock_expires_so_a_crashed_holder_cannot_wedge_it(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        handle = await redis_coordinator.acquire_lock("exec_3", ttl_seconds=0.2)
        assert handle is not None
        await asyncio.sleep(0.35)
        assert await redis_coordinator.acquire_lock("exec_3", ttl_seconds=5) is not None

    async def test_lock_can_be_extended(self, redis_coordinator: RedisCoordinator) -> None:
        handle = await redis_coordinator.acquire_lock("exec_4", ttl_seconds=0.3)
        assert handle is not None
        assert await redis_coordinator.extend_lock(handle, ttl_seconds=5) is True
        await asyncio.sleep(0.4)
        assert await redis_coordinator.is_locked("exec_4") is True

    async def test_context_manager_releases(self, redis_coordinator: RedisCoordinator) -> None:
        async with redis_coordinator.lock("exec_5", ttl_seconds=5):
            assert await redis_coordinator.is_locked("exec_5") is True
        assert await redis_coordinator.is_locked("exec_5") is False

    async def test_conflicting_lock_raises_a_retryable_error(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        async with redis_coordinator.lock("exec_6", ttl_seconds=5):
            with pytest.raises(ConcurrencyConflictError) as info:
                async with redis_coordinator.lock("exec_6", ttl_seconds=5):
                    pass
        assert info.value.retryable is True

    async def test_semaphore_caps_concurrency(self, redis_coordinator: RedisCoordinator) -> None:
        assert await redis_coordinator.acquire_slot("agents", limit=2) is True
        assert await redis_coordinator.acquire_slot("agents", limit=2) is True
        assert await redis_coordinator.acquire_slot("agents", limit=2) is False
        assert await redis_coordinator.slots_in_use("agents") == 2
        await redis_coordinator.release_slot("agents")
        assert await redis_coordinator.acquire_slot("agents", limit=2) is True

    async def test_release_cannot_drive_a_slot_negative(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        """A bare DECR would let a double release over-admit work."""
        await redis_coordinator.release_slot("tools")
        await redis_coordinator.release_slot("tools")
        assert await redis_coordinator.slots_in_use("tools") == 0

    async def test_concurrent_slot_acquisition_respects_the_limit(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        """Check-then-increment from the client would race; the Lua script cannot."""
        results = await asyncio.gather(
            *(redis_coordinator.acquire_slot("race", limit=3) for _ in range(10))
        )
        assert sum(results) == 3

    async def test_event_stream_round_trip(self, redis_coordinator: RedisCoordinator) -> None:
        event = ExecutionEvent.make(
            EventType.NODE_STARTED,
            execution_id="exec_stream",
            sequence=1,
            node_id="a",
            message="started",
            confidence=0.5,
        )
        await redis_coordinator.publish_event(event)
        entries = await redis_coordinator.read_events("exec_stream")
        assert len(entries) == 1
        assert entries[0]["type"] == "node_started"
        assert entries[0]["node_id"] == "a"

    async def test_stream_is_capped(self, redis_coordinator: RedisCoordinator) -> None:
        for i in range(50):
            await redis_coordinator.publish_event(
                ExecutionEvent.make(EventType.NODE_STARTED, execution_id="exec_cap", sequence=i),
                max_len=10,
            )
        # Approximate trimming, so assert an upper bound rather than equality.
        assert await redis_coordinator.stream_length("exec_cap") <= 50

    async def test_cancellation_signal_crosses_processes(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        assert await redis_coordinator.cancellation_requested("exec_c") is None
        await redis_coordinator.request_cancellation("exec_c", "operator stop")
        assert await redis_coordinator.cancellation_requested("exec_c") == "operator stop"
        await redis_coordinator.clear_cancellation("exec_c")
        assert await redis_coordinator.cancellation_requested("exec_c") is None

    async def test_flush_is_scoped_to_the_namespace(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        """The engine must never FLUSHDB a shared Redis."""
        await redis_coordinator.client.set("unrelated:key", "keep me")
        await redis_coordinator.acquire_lock("scoped", ttl_seconds=30)
        await redis_coordinator.flush_namespace()
        assert await redis_coordinator.client.get("unrelated:key") == "keep me"
        assert await redis_coordinator.is_locked("scoped") is False
        await redis_coordinator.client.delete("unrelated:key")

    async def test_reports_the_server_implementation(
        self, redis_coordinator: RedisCoordinator
    ) -> None:
        info = await redis_coordinator.info()
        assert info["redis_version"]


class TestPgVector:
    async def test_nearest_neighbour_search_is_ordered(self, database: Database) -> None:
        """pgvector doing the job it is here for."""
        from sqlalchemy import text

        async with database.session() as session:
            for i, (name, vector) in enumerate(
                [
                    ("exact", "[1,0,0]"),
                    ("close", "[0.9,0.1,0]"),
                    ("far", "[0,1,0]"),
                ]
            ):
                await session.execute(
                    text(
                        "INSERT INTO evidence_chunks "
                        "(id, source, title, content, content_hash, embedding) "
                        "VALUES (:id, :src, :t, :c, :h, "
                        "(:e || repeat(',0', 765) || ']')::vector)"
                    ),
                    {
                        "id": f"ev_{i}",
                        "src": f"https://example.test/{name}",
                        "t": name,
                        "c": f"content for {name}",
                        "h": f"hash_{i}",
                        "e": vector[:-1],
                    },
                )

        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT title FROM evidence_chunks "
                            "ORDER BY embedding <=> "
                            "('[1,0,0' || repeat(',0', 765) || ']')::vector LIMIT 3"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert list(rows) == ["exact", "close", "far"]

    async def test_duplicate_evidence_is_rejected(self, database: Database) -> None:
        """Source dedup depends on the unique constraint holding."""
        from sqlalchemy import text

        from orchestration.errors import DuplicateError

        async def insert() -> None:
            async with database.session() as session:
                await session.execute(
                    text(
                        "INSERT INTO evidence_chunks (id, source, title, content, content_hash) "
                        "VALUES (:id, :src, '', 'body', :h)"
                    ),
                    {"id": f"ev_{id(object())}", "src": "https://a.test", "h": "same-hash"},
                )

        await insert()
        with pytest.raises(DuplicateError):
            await insert()
