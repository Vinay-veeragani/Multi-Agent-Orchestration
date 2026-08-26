"""Shared pytest fixtures.

Integration fixtures (PostgreSQL, Redis) live in :mod:`tests.integration.conftest`
so that the unit suite never touches a network socket and stays fast.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from orchestration.domain import (
    AgentCapability,
    AgentDefinition,
    Condition,
    ExecutionState,
    JoinPolicy,
    NodeCondition,
    NodeKind,
    Task,
    ToolPermission,
    ToolSpec,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)


@pytest.fixture
def rng() -> random.Random:
    """Seeded RNG so jittered backoff assertions are deterministic."""
    return random.Random(1337)


@pytest.fixture
def task() -> Task:
    return Task(
        description="Compare the top CRM vendors on pricing and AI features.",
        success_criteria=("names at least 5 vendors", "cites a source per price"),
    )


@pytest.fixture
def linear_workflow() -> Workflow:
    """A -> B -> C."""
    return Workflow(
        name="linear",
        nodes=(
            WorkflowNode(id="a", kind=NodeKind.AGENT, agent_id="research_agent", output_key="a"),
            WorkflowNode(id="b", kind=NodeKind.AGENT, agent_id="analyst_agent", output_key="b"),
            WorkflowNode(id="c", kind=NodeKind.TERMINAL),
        ),
        edges=(
            WorkflowEdge(source="a", target="b"),
            WorkflowEdge(source="b", target="c"),
        ),
    )


@pytest.fixture
def diamond_workflow() -> Workflow:
    """Fan-out to two agents, fan-in at a join, then finish.

    This is the canonical parallel shape the engine must handle:
    ``a -> (b, c) -> join -> d``.
    """
    return Workflow(
        name="diamond",
        nodes=(
            WorkflowNode(id="a", kind=NodeKind.AGENT, agent_id="research_agent", output_key="a"),
            WorkflowNode(id="b", kind=NodeKind.AGENT, agent_id="pricing_agent", output_key="b"),
            WorkflowNode(id="c", kind=NodeKind.AGENT, agent_id="feature_agent", output_key="c"),
            WorkflowNode(id="join", kind=NodeKind.JOIN, join_policy=JoinPolicy.ALL),
            WorkflowNode(id="d", kind=NodeKind.AGENT, agent_id="analyst_agent", output_key="d"),
            WorkflowNode(id="end", kind=NodeKind.TERMINAL),
        ),
        edges=(
            WorkflowEdge(source="a", target="b"),
            WorkflowEdge(source="a", target="c"),
            WorkflowEdge(source="b", target="join"),
            WorkflowEdge(source="c", target="join"),
            WorkflowEdge(source="join", target="d"),
            WorkflowEdge(source="d", target="end"),
        ),
    )


@pytest.fixture
def conditional_workflow() -> Workflow:
    """Branch on confidence: low -> more research, high -> finalize."""
    low = NodeCondition(conditions=(Condition(path="confidence.a", operator="lt", value=0.6),))
    high = NodeCondition(conditions=(Condition(path="confidence.a", operator="gte", value=0.6),))
    return Workflow(
        name="conditional",
        nodes=(
            WorkflowNode(id="a", kind=NodeKind.AGENT, agent_id="research_agent", output_key="a"),
            WorkflowNode(
                id="more", kind=NodeKind.AGENT, agent_id="research_agent", output_key="more"
            ),
            WorkflowNode(id="final", kind=NodeKind.AGENT, agent_id="finalizer_agent"),
            WorkflowNode(id="end", kind=NodeKind.TERMINAL),
        ),
        edges=(
            WorkflowEdge(source="a", target="more", condition=low, label="low confidence"),
            WorkflowEdge(source="a", target="final", condition=high, label="confident"),
            WorkflowEdge(source="more", target="final"),
            WorkflowEdge(source="final", target="end"),
        ),
    )


@pytest.fixture
def execution_state(task: Task, linear_workflow: Workflow) -> ExecutionState:
    return ExecutionState(
        execution_id="exec_test",
        workflow_id=linear_workflow.id,
        task=task,
    )


@pytest.fixture
def research_agent() -> AgentDefinition:
    return AgentDefinition(
        id="research_agent",
        name="Research Agent",
        description="Finds sources and extracts citations.",
        kind="research",
        system_prompt="You research topics and always cite sources.",
        capabilities=(
            AgentCapability(
                name="web_research",
                description="Search the web for sources",
                keywords=frozenset({"research", "search", "find", "compare"}),
                proficiency=0.9,
            ),
        ),
        allowed_tools=(
            ToolPermission(tool="web_search"),
            ToolPermission(tool="read_file", constraints={"path": {"prefix": "./data"}}),
        ),
    )


@pytest.fixture
def calculator_tool() -> ToolSpec:
    return ToolSpec(
        name="calculator",
        description="Evaluate an arithmetic expression.",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
        tags=frozenset({"math", "pure"}),
    )


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Ensure a test that monkeypatches the environment cannot leak settings."""
    from orchestration.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()
