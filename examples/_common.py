"""Shared scaffolding for the demo scripts under ``examples/``.

Every demo builds a real engine -- real PostgreSQL, real Redis, the real
:class:`ExecutionOrchestrator` -- and narrates what happens as it happens by
subscribing an :class:`~orchestration.events.bus.EventSink` to the run. The
only thing standing in for something real is the LLM: no API key has been
configured yet, so every demo scripts a :class:`MockProvider` with a plausible
trajectory and says so plainly in its own banner. Swapping in a real provider
later needs no change here -- ``AppState``-style wiring in
:mod:`orchestration.api.state` is the template for that, once credentials
exist.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from orchestration.agents.definitions import build_default_agent_registry
from orchestration.agents.registry import AgentRegistry
from orchestration.agents.runtime import AgentRuntime
from orchestration.budget.meter import BudgetGuard, BudgetMeter
from orchestration.checkpoint.manager import (
    CheckpointManager,
    restore_status_for_resume,
    resume_execution,
)
from orchestration.config import Settings, get_settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.base import JsonDict, new_id
from orchestration.domain.budget import UNLIMITED_BUDGET, Budget, BudgetUsage
from orchestration.domain.enums import EventType, PolicyEffect
from orchestration.domain.events import ExecutionEvent
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task, Workflow
from orchestration.events.bus import EventBus, EventSink, ExecutionEventRecorder
from orchestration.events.sinks import PostgresEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import ExecutionRepository, WorkflowRepository
from orchestration.policies.approvals import ApprovalService
from orchestration.policies.engine import PolicyEngine, build_default_policy_engine
from orchestration.routing.model_router import build_default_router
from orchestration.runtime.orchestrator import ExecutionOrchestrator, seed_dynamic_workflow
from orchestration.supervisor.supervisor import Supervisor
from orchestration.tools.registry import ToolRegistry, build_default_registry

#: Printed at the top of every demo, so a reader never mistakes the scripted
#: trajectory below for a real model's judgement.
MOCK_NOTICE = (
    "This demo scripts a MockProvider -- no LLM API key is configured yet. "
    "The supervisor decisions and agent outputs below are a plausible scripted "
    "trajectory, not a real model's output. The engine underneath (routing, "
    "the workflow graph it builds, checkpointing, budget enforcement, the "
    "approval gate) is entirely real, running against your local PostgreSQL "
    "and Redis."
)


async def _no_sleep(delay: float) -> None:
    return None


def common_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--test-db",
        action="store_true",
        help="Use the test database/Redis namespace instead of the configured ones.",
    )
    return parser


class NarratingSink(EventSink):
    """Prints each event as it happens, so the demo reads like a transcript."""

    _LABELS: ClassVar[dict[EventType, str]] = {
        EventType.EXECUTION_STARTED: "started",
        EventType.SUPERVISOR_DECIDED: "supervisor",
        EventType.NODE_STARTED: "node",
        EventType.AGENT_INVOKED: "agent",
        EventType.NODE_COMPLETED: "node ok",
        EventType.NODE_FAILED: "node failed",
        EventType.RETRY_STARTED: "retry",
        EventType.APPROVAL_REQUESTED: "approval requested",
        EventType.APPROVAL_GRANTED: "approval granted",
        EventType.APPROVAL_REJECTED: "approval rejected",
        EventType.EXECUTION_COMPLETED: "completed",
        EventType.EXECUTION_FAILED: "failed",
    }

    async def emit(self, event: ExecutionEvent) -> None:
        label = self._LABELS.get(event.type, event.type.value)
        detail = event.message or ""
        who = event.agent_id or event.node_id or ""
        print(f"  [{label:>20s}] {who:20s} {detail}")


@dataclass(slots=True)
class Engine:
    """Every collaborator a demo needs, built once and reused across steps."""

    settings: Settings
    database: Database
    redis: RedisCoordinator
    agents: AgentRegistry
    tools: ToolRegistry
    policy: PolicyEngine
    provider: MockProvider
    checkpoint_manager: CheckpointManager
    sandbox_root: Path

    async def aclose(self) -> None:
        await self.database.aclose()
        await self.redis.aclose()


async def build_engine(
    *, test_db: bool, provider: MockProvider, sandbox_root: Path
) -> Engine:
    settings = get_settings()
    dsn = settings.pg_test_dsn if test_db else settings.pg_dsn
    redis_url = settings.redis_test_url if test_db else settings.redis_url
    namespace = f"{settings.redis_namespace}_demo" if test_db else settings.redis_namespace

    database = Database(dsn, settings=settings)
    redis = RedisCoordinator(redis_url, namespace=namespace, settings=settings)
    agents = build_default_agent_registry()
    tools = build_default_registry()
    policy = build_default_policy_engine(agents=agents, tools=tools)

    return Engine(
        settings=settings,
        database=database,
        redis=redis,
        agents=agents,
        tools=tools,
        policy=policy,
        provider=provider,
        checkpoint_manager=CheckpointManager(database),
        sandbox_root=sandbox_root,
    )


@dataclass(slots=True)
class DemoRun:
    """A dynamic execution set up and ready to go, plus what resuming it needs."""

    orchestrator: ExecutionOrchestrator
    state: ExecutionState
    workflow: Workflow
    approvals: ApprovalService


def _build_orchestrator(
    engine: Engine,
    execution_id: str,
    budget_usage: BudgetUsage,
    budget: Budget,
    *,
    event_sequence: int = 0,
    on_event: Callable[[ExecutionEvent], Awaitable[None]] | None = None,
) -> tuple[ExecutionOrchestrator, ApprovalService]:
    """Wire a fresh orchestrator (and the approval service alongside it).

    Used both to start a run and, with a nonzero ``event_sequence``, to
    reconstruct one for resume -- every collaborator here is rebuilt from
    scratch each time, exactly as a genuinely separate process would.
    """
    llm = LLMClient.mock(engine.provider, sleep=_no_sleep)
    router = build_default_router(mock_only=True, configured_providers=["mock"])

    sinks: list[EventSink] = [NarratingSink(), PostgresEventSink(engine.database)]
    bus = EventBus(sinks, start_sequence=event_sequence)
    if on_event is not None:
        bus.add_sink(_CallbackSink(on_event))
    events = ExecutionEventRecorder(bus=bus, execution_id=execution_id)
    approvals = ApprovalService(engine.database, events=bus)
    meter = BudgetMeter(budget, budget_usage)

    async def authorise(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
        decision = engine.policy.evaluate(agent_id, tool, arguments)
        if decision.allowed:
            engine.policy.record_call(agent_id, tool)
        return decision.effect, decision.reason

    runtime = AgentRuntime(
        llm=llm,
        tools=engine.tools,
        router=router,
        authoriser=approvals.tool_authoriser(  # type: ignore[arg-type]
            authorise, execution_id=execution_id
        ),
        budget_check=BudgetGuard(meter),
    )
    supervisor = Supervisor(agents=engine.agents, llm=llm, router=router, tools=engine.tools)
    orchestrator = ExecutionOrchestrator(
        supervisor=supervisor,
        agents=engine.agents,
        tools=engine.tools,
        runtime=runtime,
        approvals=approvals,
        meter=meter,
        events=events,
        checkpoint=engine.checkpoint_manager.writer(),  # type: ignore[arg-type]
        sandbox_root=engine.sandbox_root,
    )
    return orchestrator, approvals


async def start_dynamic_run(
    engine: Engine,
    task: str,
    *,
    success_criteria: tuple[str, ...] = (),
    budget: Budget = UNLIMITED_BUDGET,
    on_event: Callable[[ExecutionEvent], Awaitable[None]] | None = None,
) -> DemoRun:
    """Seed and prepare (but do not yet run) a dynamic, supervisor-driven execution."""
    workflow = seed_dynamic_workflow()

    async with engine.database.session() as session:
        await WorkflowRepository(session).save(workflow)
        execution_id = await ExecutionRepository(session).create(
            execution_id=new_id("execution"),
            workflow_id=workflow.id,
            task_description=task,
        )

    state = ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow.id,
        task=Task(description=task, success_criteria=success_criteria),
        budget=budget,
        budget_usage=BudgetUsage(),
    )
    orchestrator, approvals = _build_orchestrator(
        engine, execution_id, state.budget_usage, budget, on_event=on_event
    )
    return DemoRun(orchestrator=orchestrator, state=state, workflow=workflow, approvals=approvals)


async def resume_dynamic_run(
    engine: Engine,
    execution_id: str,
    *,
    on_event: Callable[[ExecutionEvent], Awaitable[None]] | None = None,
) -> DemoRun:
    """Reconstruct a paused execution from durable state alone.

    Nothing here is carried over in memory from whatever process started the
    run -- ``resume_execution`` reads the checkpoint, and every collaborator
    is rebuilt fresh, exactly as it would be after a genuine restart.
    """
    context = await resume_execution(engine.checkpoint_manager, execution_id, require_claim=False)
    await restore_status_for_resume(context.state)
    orchestrator, approvals = _build_orchestrator(
        engine,
        execution_id,
        context.state.budget_usage,
        context.state.budget,
        event_sequence=context.event_sequence,
        on_event=on_event,
    )
    return DemoRun(
        orchestrator=orchestrator,
        state=context.state,
        workflow=context.workflow,
        approvals=approvals,
    )


class _CallbackSink(EventSink):
    def __init__(self, callback: Callable[[ExecutionEvent], Awaitable[None]]) -> None:
        self._callback = callback

    async def emit(self, event: ExecutionEvent) -> None:
        await self._callback(event)


def print_banner(title: str) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(MOCK_NOTICE)
    print()


def print_final(state: ExecutionState) -> None:
    print()
    print("-" * 78)
    print(f"status: {state.status.value}")
    if state.final_output:
        print("final output:")
        print(state.final_output)
    if state.failure_reason:
        print(f"failure reason: {state.failure_reason}")
    print(
        f"tokens={state.budget_usage.total_tokens} "
        f"cost=${state.budget_usage.cost_usd:.4f} "
        f"agent_steps={state.budget_usage.agent_steps} "
        f"retries={state.budget_usage.retries}"
    )
    print("-" * 78)


def run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)  # type: ignore[arg-type]
