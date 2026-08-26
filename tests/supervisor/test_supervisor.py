"""Tests for the supervisor: structured routing, semantic validation, fallback.

The property that matters most: **the engine never receives a decision that has
not passed all three layers.** A schema-valid decision naming a nonexistent agent
must not reach the executor, and an unusable model must degrade rather than
deadlock.
"""

from __future__ import annotations

import pytest

from orchestration.agents.definitions import build_default_agent_registry
from orchestration.agents.registry import AgentRegistry
from orchestration.domain.enums import NodeKind, NodeStatus, SupervisorAction
from orchestration.domain.execution import ExecutionState
from orchestration.domain.routing import RoutingDecision
from orchestration.domain.workflow import (
    DynamicPlan,
    Task,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from orchestration.errors import InputValidationError
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import (
    CONTENT_FAULT_PAYLOADS,
    Fault,
    MockProvider,
    MockRule,
    routing_decision,
)
from orchestration.routing.model_router import build_default_router
from orchestration.supervisor.heuristic import HeuristicRouter
from orchestration.supervisor.supervisor import MAX_REPLANS, Supervisor
from orchestration.tools.registry import build_default_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def agents() -> AgentRegistry:
    return build_default_agent_registry()


@pytest.fixture
def state() -> ExecutionState:
    return ExecutionState(
        execution_id="exec_sup",
        workflow_id="wkf_1",
        task=Task(
            description="Compare the top CRM vendors on pricing and AI features.",
            success_criteria=("names at least 5 vendors",),
        ),
    )


def _supervisor(
    agents: AgentRegistry,
    provider: MockProvider,
    **kwargs: object,
) -> Supervisor:
    return Supervisor(
        agents=agents,
        llm=LLMClient.mock(provider, sleep=_no_sleep),
        router=build_default_router(),
        tools=build_default_registry(),
        **kwargs,  # type: ignore[arg-type]
    )


async def _no_sleep(delay: float) -> None:
    return None


class TestStructuredRouting:
    async def test_parallel_delegate_is_accepted(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(
                        routing_decision(
                            "parallel_delegate",
                            agents=["research_agent", "pricing_agent", "feature_agent"],
                            reason="three independent research dimensions",
                            confidence=0.91,
                        ),
                    ),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.decision.action is SupervisorAction.PARALLEL_DELEGATE
        assert outcome.decision.agent_ids == (
            "research_agent",
            "pricing_agent",
            "feature_agent",
        )
        assert outcome.degraded is False
        assert outcome.used_fallback is False

    async def test_delegate_is_accepted(self, agents: AgentRegistry, state: ExecutionState) -> None:
        provider = MockProvider(
            [MockRule(name="sup", responses=(routing_decision("delegate", agents=["data_agent"]),))]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.decision.action is SupervisorAction.DELEGATE

    async def test_respond_directly_is_accepted(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(routing_decision("respond_directly", answer="42"),),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.decision.action is SupervisorAction.RESPOND_DIRECTLY
        assert outcome.decision.answer == "42"

    async def test_costs_are_recorded(self, agents: AgentRegistry, state: ExecutionState) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(routing_decision("finalize", answer="done"),),
                    usage=(1200, 80),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.total_tokens == 1280
        assert outcome.attempt_count == 1

    async def test_prompt_describes_available_agents(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [MockRule(name="sup", responses=(routing_decision("finalize", answer="x"),))]
        )
        await _supervisor(agents, provider).decide(state)
        call = provider.calls[0]
        assert "supervisor" in call.system_preview.lower()

    async def test_shortlisting_reduces_the_prompt(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """Routing runs every step, so prompt size is a recurring cost."""
        full = MockProvider(
            [MockRule(name="sup", responses=(routing_decision("finalize", answer="x"),))]
        )
        short = MockProvider(
            [MockRule(name="sup", responses=(routing_decision("finalize", answer="x"),))]
        )
        await _supervisor(agents, full).decide(state)
        await _supervisor(agents, short, shortlist_size=2).decide(state)
        assert short.calls[0].input_tokens < full.calls[0].input_tokens


class TestSchemaRepair:
    async def test_malformed_json_is_repaired_and_marked_degraded(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(
                        CONTENT_FAULT_PAYLOADS["malformed_json"],
                        routing_decision("delegate", agents=["research_agent"]),
                    ),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.decision.action is SupervisorAction.DELEGATE
        assert outcome.degraded is True, "a repair must be visible, not smoothed over"
        assert outcome.used_fallback is False

    async def test_prose_reply_falls_back_to_the_heuristic(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """An unusable model degrades; it does not stall the execution."""
        provider = MockProvider(
            [MockRule(name="sup", responses=(CONTENT_FAULT_PAYLOADS["prose_instead_of_json"],))]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.used_fallback is True
        assert outcome.degraded is True
        assert outcome.decision.requires_agents or outcome.decision.is_terminal
        assert "[fallback]" in outcome.decision.reason

    async def test_provider_outage_falls_back(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [MockRule(name="sup", fault=Fault("provider_unavailable", attempts=(1, 2, 3, 4)))]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.used_fallback is True
        assert outcome.attempts[-1].from_fallback is True

    async def test_fallback_records_why_it_happened(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider([MockRule(name="sup", responses=("not json at all",))])
        outcome = await _supervisor(agents, provider).decide(state)
        reasons = outcome.attempts[-1].validation_errors
        assert reasons and any("valid decision" in r or "validation" in r for r in reasons)


class TestSemanticValidation:
    """The layer a JSON schema structurally cannot provide."""

    async def test_hallucinated_agent_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(routing_decision("delegate", agents=["ghost_agent"]),),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.used_fallback is True, "a nonexistent agent reached the executor"
        assert "unknown agent" in outcome.attempts[-1].validation_errors[0]

    async def test_disabled_agent_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        agents.update("pricing_agent", enabled=False)
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(routing_decision("delegate", agents=["pricing_agent"]),),
                )
            ]
        )
        outcome = await _supervisor(agents, provider).decide(state)
        assert outcome.used_fallback is True
        assert "disabled" in outcome.attempts[-1].validation_errors[0]

    def test_retry_of_a_node_that_never_ran_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.RETRY, reason="r", retry_node_id="never_ran"
        )
        problems = supervisor.validate_decision(decision, state)
        assert problems
        assert "never run" in problems[0]

    def test_retry_of_a_succeeded_node_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        state.node_state("research").mark_succeeded()
        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.RETRY, reason="r", retry_node_id="research"
        )
        problems = supervisor.validate_decision(decision, state)
        assert "already succeeded" in problems[0]

    def test_retry_of_a_terminal_failure_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """Retrying a permission denial would repeat a guaranteed failure."""
        node = state.node_state("research")
        node.mark_running()
        node.mark_failed({"code": "permission_denied", "message": "no", "retryable": False})
        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.RETRY, reason="r", retry_node_id="research"
        )
        problems = supervisor.validate_decision(decision, state)
        assert "terminal" in problems[0]

    def test_retry_of_a_retryable_failure_is_permitted(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        node = state.node_state("research")
        node.mark_running()
        node.mark_failed({"code": "timeout", "message": "slow", "retryable": True})
        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.RETRY, reason="r", retry_node_id="research"
        )
        assert supervisor.validate_decision(decision, state) == []

    def test_excessive_fan_out_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """An unbounded fan-out exhausts the budget in a single step."""
        from orchestration.domain.routing import DelegationTarget

        for i in range(12):
            agents.register(agents.get("research_agent").merged(id=f"clone_{i}", name=f"Clone {i}"))
        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.PARALLEL_DELEGATE,
            reason="r",
            targets=tuple(
                DelegationTarget(agent_id=f"clone_{i}", instruction="go") for i in range(12)
            ),
        )
        problems = supervisor.validate_decision(decision, state)
        assert any("exhaust the budget" in p for p in problems)

    def test_all_problems_are_reported_together(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        from orchestration.domain.routing import DelegationTarget

        supervisor = _supervisor(agents, MockProvider())
        decision = RoutingDecision(
            action=SupervisorAction.PARALLEL_DELEGATE,
            reason="r",
            targets=(
                DelegationTarget(agent_id="ghost_one", instruction="go"),
                DelegationTarget(agent_id="ghost_two", instruction="go"),
            ),
        )
        problems = supervisor.validate_decision(decision, state)
        assert len(problems) == 2


class TestPlanValidation:
    def test_plan_naming_an_unknown_agent_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """A supervisor-invented plan is untrusted input, like a user's."""
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="new", kind=NodeKind.AGENT, agent_id="ghost_agent"),)
        )
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        problems = supervisor.validate_decision(decision, state)
        assert any("unknown agent" in p for p in problems)

    def test_plan_naming_an_unknown_tool_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(nodes=(WorkflowNode(id="new", kind=NodeKind.TOOL, tool="ghost_tool"),))
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        problems = supervisor.validate_decision(decision, state)
        assert any("unknown tool" in p for p in problems)

    def test_plan_colliding_with_an_existing_node_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState, linear_workflow: Workflow
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="a", kind=NodeKind.AGENT, agent_id="critic_agent"),)
        )
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        problems = supervisor.validate_decision(decision, state, workflow=linear_workflow)
        assert any("collides" in p for p in problems)

    def test_plan_attaching_to_an_unknown_node_is_rejected(
        self, agents: AgentRegistry, state: ExecutionState, linear_workflow: Workflow
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="new", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            attach_after=("nowhere",),
        )
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        problems = supervisor.validate_decision(decision, state, workflow=linear_workflow)
        assert any("unknown node" in p for p in problems)

    def test_replan_limit_stops_plan_churn(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """Replanning is legitimate; replanning forever is a failure mode."""
        state.replan_count = MAX_REPLANS
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="new", kind=NodeKind.AGENT, agent_id="critic_agent"),)
        )
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        problems = supervisor.validate_decision(decision, state)
        assert any("replan limit" in p for p in problems)

    def test_a_valid_plan_passes(
        self, agents: AgentRegistry, state: ExecutionState, linear_workflow: Workflow
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="critique", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            attach_after=("b",),
        )
        decision = RoutingDecision(action=SupervisorAction.REPLAN, reason="r", plan=plan)
        assert supervisor.validate_decision(decision, state, workflow=linear_workflow) == []


class TestPlanCompilation:
    def test_plan_is_merged_and_wired_to_attachment_points(
        self, agents: AgentRegistry, linear_workflow: Workflow
    ) -> None:
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="critique", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            attach_after=("b",),
        )
        merged = supervisor.compile_plan(plan, linear_workflow)
        assert "critique" in merged.node_map
        assert any(e.source == "b" and e.target == "critique" for e in merged.edges)
        assert merged.dynamic is True

    def test_the_original_workflow_is_untouched(
        self, agents: AgentRegistry, linear_workflow: Workflow
    ) -> None:
        """The pre-replan graph must survive in the checkpoint history."""
        supervisor = _supervisor(agents, MockProvider())
        original_nodes = len(linear_workflow.nodes)
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="critique", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            attach_after=("b",),
        )
        supervisor.compile_plan(plan, linear_workflow)
        assert len(linear_workflow.nodes) == original_nodes

    def test_compilation_rejects_a_graph_that_would_cycle(
        self, agents: AgentRegistry, linear_workflow: Workflow
    ) -> None:
        """Final guard: an invalid graph must never reach the scheduler."""
        supervisor = _supervisor(agents, MockProvider())
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="loop", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            edges=(
                WorkflowEdge(source="a", target="loop"),
                WorkflowEdge(source="loop", target="a"),
            ),
            attach_after=("a",),
        )
        with pytest.raises(InputValidationError) as info:
            supervisor.compile_plan(plan, linear_workflow)
        assert "problems" in info.value.context or info.value.context


class TestHeuristicRouter:
    """The fallback must be correct on its own, not merely present."""

    def test_delegates_to_the_single_best_match(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        state.task = Task(description="profile this csv dataset and compute the median")
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is SupervisorAction.DELEGATE
        assert decision.agent_ids == ("data_agent",)

    def test_fans_out_when_several_agents_score_closely(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is SupervisorAction.PARALLEL_DELEGATE
        assert len(decision.targets) >= 2

    def test_fails_when_nothing_matches_and_nothing_is_done(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        state.task = Task(description="qwertyuiop zxcvbnm")
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is SupervisorAction.FAIL
        assert decision.failure_reason

    def test_finalizes_from_completed_work_when_nothing_matches(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        state.task = Task(description="qwertyuiop zxcvbnm")
        state.record_agent_output(
            "research",
            {"content": "found five vendors", "confidence": 0.8, "evidence": ["https://a"]},
            output_key=None,
        )
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is SupervisorAction.FINALIZE
        assert decision.answer is not None
        assert "found five vendors" in decision.answer
        assert "https://a" in decision.answer

    def test_summary_is_labelled_as_unsynthesised(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """The fallback must not present a raw concatenation as a polished report."""
        state.task = Task(description="qwertyuiop")
        state.record_agent_output("research", {"content": "x", "confidence": 0.5}, output_key=None)
        decision = HeuristicRouter(agents).decide(state)
        assert decision.answer is not None
        assert "without model synthesis" in decision.answer

    def test_retries_a_retryable_failure_first(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        node = state.node_state("research")
        node.mark_running()
        node.mark_failed({"code": "timeout", "message": "slow", "retryable": True})
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is SupervisorAction.RETRY
        assert decision.retry_node_id == "research"

    def test_does_not_retry_a_terminal_failure(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        node = state.node_state("research")
        node.mark_running()
        node.mark_failed({"code": "permission_denied", "message": "no", "retryable": False})
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action is not SupervisorAction.RETRY

    def test_does_not_redelegate_to_a_used_agent(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """The obvious failure mode of a stateless heuristic."""
        state.task = Task(description="find the price per seat of each subscription tier")
        first = HeuristicRouter(agents).decide(state)
        for target in first.targets:
            state.node_state(target.agent_id).mark_succeeded()
            state.record_agent_output(target.agent_id, {"content": "x"}, output_key=None)
        second = HeuristicRouter(agents).decide(state)
        assert not set(second.agent_ids) & set(first.agent_ids)

    def test_gaps_steer_the_follow_up_instruction(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """After a research pass, the gaps describe the work better than the task."""
        state.record_agent_output(
            "research",
            {
                "content": "partial",
                "confidence": 0.4,
                "gaps": ["no pricing found for Zoho"],
            },
            output_key=None,
        )
        decision = HeuristicRouter(agents).decide(state)
        if decision.targets:
            assert "no pricing found for Zoho" in decision.targets[0].instruction

    def test_confidence_is_middling_not_invented(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """Claiming high confidence for a heuristic would corrupt downstream branching."""
        decision = HeuristicRouter(agents).decide(state)
        assert 0.2 <= decision.confidence <= 0.6

    def test_decisions_are_deterministic(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        router = HeuristicRouter(agents)
        first = router.decide(state)
        second = router.decide(state)
        assert first.action is second.action
        assert first.agent_ids == second.agent_ids

    def test_every_fallback_decision_is_schema_valid(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """A fallback that produced an invalid decision would defeat its purpose."""
        router = HeuristicRouter(agents)
        scenarios = [
            "compare CRM vendors on pricing",
            "profile this csv",
            "qwertyuiop",
            "audit this report for unsupported claims",
        ]
        for description in scenarios:
            state.task = Task(description=description)
            decision = router.decide(state)
            RoutingDecision.model_validate(decision.model_dump())


class TestNoAgentsRegistered:
    async def test_supervisor_copes_with_an_empty_registry(self, state: ExecutionState) -> None:
        """An empty registry must not crash routing."""
        empty = AgentRegistry()
        provider = MockProvider(
            [
                MockRule(
                    name="sup",
                    responses=(routing_decision("respond_directly", answer="nothing to delegate"),),
                )
            ]
        )
        supervisor = Supervisor(
            agents=empty,
            llm=LLMClient.mock(provider, sleep=_no_sleep),
            router=build_default_router(),
        )
        outcome = await supervisor.decide(state)
        assert outcome.decision.action is SupervisorAction.RESPOND_DIRECTLY

    def test_heuristic_fails_cleanly_with_no_agents(self, state: ExecutionState) -> None:
        decision = HeuristicRouter(AgentRegistry()).decide(state)
        assert decision.action is SupervisorAction.FAIL


class TestNodeStatusHandling:
    def test_running_nodes_count_as_used(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        """A node already in flight must not be delegated again."""
        state.node_state("pricing_agent").mark_running()
        decision = HeuristicRouter(agents).decide(state)
        assert "pricing_agent" not in decision.agent_ids

    def test_skipped_nodes_do_not_block_delegation(
        self, agents: AgentRegistry, state: ExecutionState
    ) -> None:
        state.node_state("some_other_node").mark_skipped("branch not taken")
        decision = HeuristicRouter(agents).decide(state)
        assert decision.action in {
            SupervisorAction.DELEGATE,
            SupervisorAction.PARALLEL_DELEGATE,
        }
        assert NodeStatus.SKIPPED is state.node_states["some_other_node"].status
