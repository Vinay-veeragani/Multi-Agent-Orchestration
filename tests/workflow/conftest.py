"""Fixtures for workflow executor tests.

Builds a complete in-memory engine: mock provider, real registries, real policy
engine, real budget meter, real event bus. Nothing here is a stub except the LLM
itself, so these tests exercise the actual scheduler.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orchestration.agents.definitions import build_default_agent_registry
from orchestration.agents.registry import AgentRegistry
from orchestration.agents.runtime import AgentRuntime
from orchestration.budget.meter import BudgetGuard, BudgetMeter
from orchestration.domain.base import JsonDict
from orchestration.domain.budget import UNLIMITED_BUDGET, Budget, BudgetUsage
from orchestration.domain.checkpoint import Checkpoint
from orchestration.domain.enums import CheckpointReason, PolicyEffect
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolResult
from orchestration.domain.workflow import Task, Workflow
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import MockProvider
from orchestration.policies.engine import PolicyEngine, build_default_policy_engine
from orchestration.routing.model_router import build_default_router
from orchestration.tools.registry import ToolRegistry, build_default_registry
from orchestration.workflow.executor import CancelToken, WorkflowExecutor
from orchestration.workflow.graph import WorkflowGraph


async def _no_sleep(delay: float) -> None:
    """Collapse retry backoff so failure tests stay fast."""
    return None


@dataclass
class CapturedCheckpoint:
    """One checkpoint write, recorded for assertions."""

    sequence: int
    reason: CheckpointReason
    node_id: str | None
    status: str
    checkpoint: Checkpoint


@dataclass
class Harness:
    """A complete in-memory engine, plus the recorders tests assert against."""

    provider: MockProvider
    agents: AgentRegistry
    tools: ToolRegistry
    policy: PolicyEngine
    events: InMemoryEventSink
    bus: EventBus
    meter: BudgetMeter
    runtime: AgentRuntime
    sandbox: Path
    cancel: CancelToken
    checkpoints: list[CapturedCheckpoint] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    def executor(
        self,
        workflow: Workflow,
        *,
        max_concurrent_nodes: int = 8,
        approval_gate: Callable[..., object] | None = None,
        max_steps: int = 200,
    ) -> WorkflowExecutor:
        return WorkflowExecutor(
            graph=WorkflowGraph(workflow),
            agents=self.agents,
            tools=self.tools,
            runtime=self.runtime,
            events=ExecutionEventRecorder(bus=self.bus, execution_id="exec_test"),
            meter=self.meter,
            checkpoint=self._write_checkpoint,
            approval_gate=approval_gate,  # type: ignore[arg-type]
            cancel_token=self.cancel,
            max_concurrent_nodes=max_concurrent_nodes,
            sandbox_root=self.sandbox,
            max_steps=max_steps,
        )

    def state(self, workflow: Workflow, description: str = "do the work") -> ExecutionState:
        return ExecutionState(
            execution_id="exec_test",
            workflow_id=workflow.id,
            task=Task(description=description),
            budget=self.meter.budget,
            budget_usage=self.meter.usage,
        )

    async def _write_checkpoint(
        self,
        state: ExecutionState,
        workflow: Workflow,
        reason: CheckpointReason,
        node_id: str | None,
    ) -> None:
        """In-memory checkpoint writer that also validates serialisability.

        Building a real :class:`Checkpoint` here rather than just recording the
        reason means every executor test implicitly asserts that state remains
        serialisable at every checkpoint point -- which is the property resume
        depends on.
        """
        checkpoint = Checkpoint(
            execution_id=state.execution_id,
            sequence=len(self.checkpoints),
            reason=reason,
            status=state.status,
            node_id=node_id,
            state=state.model_copy(deep=True),
            workflow=workflow,
        ).with_hash()
        self.checkpoints.append(
            CapturedCheckpoint(
                sequence=checkpoint.sequence,
                reason=reason,
                node_id=node_id,
                status=state.status.value,
                checkpoint=checkpoint,
            )
        )

    def reasons(self) -> list[CheckpointReason]:
        return [c.reason for c in self.checkpoints]

    def latest_resumable(self) -> Checkpoint | None:
        for captured in reversed(self.checkpoints):
            if captured.checkpoint.is_resumable:
                return captured.checkpoint
        return None


def build_harness(
    provider: MockProvider,
    sandbox: Path,
    *,
    budget: Budget = UNLIMITED_BUDGET,
    enable_python: bool = True,
) -> Harness:
    """Assemble an engine around ``provider``."""
    agents = build_default_agent_registry()
    tools = build_default_registry(enable_python=enable_python)
    policy = build_default_policy_engine(agents=agents, tools=tools)
    sink = InMemoryEventSink()
    bus = EventBus([sink])
    usage = BudgetUsage()
    meter = BudgetMeter(budget, usage)
    guard = BudgetGuard(meter)
    collected: list[ToolResult] = []

    async def authorise(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
        decision = policy.evaluate(agent_id, tool, arguments)
        if decision.allowed:
            policy.record_call(agent_id, tool)
        return decision.effect, decision.reason

    async def observe(agent_id: str, result: ToolResult) -> None:
        collected.append(result)

    runtime = AgentRuntime(
        llm=LLMClient.mock(provider, sleep=_no_sleep),
        tools=tools,
        router=build_default_router(),
        authoriser=authorise,
        budget_check=guard,
        tool_observer=observe,
    )

    return Harness(
        provider=provider,
        agents=agents,
        tools=tools,
        policy=policy,
        events=sink,
        bus=bus,
        meter=meter,
        runtime=runtime,
        sandbox=sandbox,
        cancel=CancelToken(),
        tool_results=collected,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def harness_factory(sandbox: Path) -> Iterator[Callable[..., Harness]]:
    """Factory so a test can build a harness with its own provider script."""

    def _factory(provider: MockProvider, **kwargs: object) -> Harness:
        return build_harness(provider, sandbox, **kwargs)  # type: ignore[arg-type]

    yield _factory
