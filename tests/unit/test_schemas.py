"""Schema-validation tests for the domain model.

These assert the *rejections*. Strict schemas are the mechanism that stops a
hallucinated or malformed LLM payload from reaching the executor, so the
important assertion is usually that something invalid raises rather than that
something valid parses.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from orchestration.domain import (
    AgentCapability,
    AgentDefinition,
    AgentOutput,
    Budget,
    BudgetDimension,
    BudgetUsage,
    Condition,
    DelegationTarget,
    DynamicPlan,
    JoinPolicy,
    Message,
    ModelCapability,
    ModelConfig,
    NodeCondition,
    NodeKind,
    Provider,
    RiskLevel,
    RoutingDecision,
    SupervisorAction,
    TokenUsage,
    ToolPermission,
    ToolResult,
    ToolSpec,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from orchestration.domain.retry import NO_RETRY_POLICY, RetryPolicy

pytestmark = pytest.mark.unit


class TestWorkflowNodeInvariants:
    """Each node kind must carry the fields its executor will dereference."""

    def test_agent_node_requires_agent_id(self) -> None:
        with pytest.raises(ValidationError, match="requires agent_id"):
            WorkflowNode(id="n", kind=NodeKind.AGENT)

    def test_tool_node_requires_tool(self) -> None:
        with pytest.raises(ValidationError, match="requires tool"):
            WorkflowNode(id="n", kind=NodeKind.TOOL)

    def test_agent_node_cannot_also_name_a_tool(self) -> None:
        with pytest.raises(ValidationError, match="cannot set both"):
            WorkflowNode(id="n", kind=NodeKind.AGENT, agent_id="a", tool="calculator")

    def test_quorum_join_requires_a_quorum(self) -> None:
        with pytest.raises(ValidationError, match="requires quorum"):
            WorkflowNode(id="n", kind=NodeKind.JOIN, join_policy=JoinPolicy.QUORUM)

    def test_quorum_is_rejected_on_non_join_nodes(self) -> None:
        with pytest.raises(ValidationError, match="not a join node"):
            WorkflowNode(id="n", kind=NodeKind.AGENT, agent_id="a", quorum=2)

    def test_terminal_node_needs_nothing_extra(self) -> None:
        node = WorkflowNode(id="end", kind=NodeKind.TERMINAL)
        assert node.effective_name == "end"

    def test_slug_pattern_is_enforced_on_ids(self) -> None:
        for bad in ("Has Spaces", "UPPER", "-leading", "sym!bol"):
            with pytest.raises(ValidationError):
                WorkflowNode(id=bad, kind=NodeKind.TERMINAL)

    def test_effective_name_prefers_the_label(self) -> None:
        node = WorkflowNode(id="n", kind=NodeKind.TERMINAL, name="Finish")
        assert node.effective_name == "Finish"


class TestWorkflowInvariants:
    def test_duplicate_node_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate node ids"):
            Workflow(
                name="w",
                nodes=(
                    WorkflowNode(id="a", kind=NodeKind.TERMINAL),
                    WorkflowNode(id="a", kind=NodeKind.TERMINAL),
                ),
            )

    def test_edges_must_reference_known_nodes(self) -> None:
        with pytest.raises(ValidationError, match="unknown node"):
            Workflow(
                name="w",
                nodes=(WorkflowNode(id="a", kind=NodeKind.TERMINAL),),
                edges=(WorkflowEdge(source="a", target="ghost"),),
            )

    def test_entry_nodes_must_exist(self) -> None:
        with pytest.raises(ValidationError, match="unknown entry nodes"):
            Workflow(
                name="w",
                nodes=(WorkflowNode(id="a", kind=NodeKind.TERMINAL),),
                entry_nodes=("nope",),
            )

    def test_at_least_one_node_is_required(self) -> None:
        with pytest.raises(ValidationError):
            Workflow(name="w", nodes=())

    def test_entry_nodes_are_inferred_from_inbound_edges(self, diamond_workflow: Workflow) -> None:
        assert diamond_workflow.resolved_entry_nodes == ("a",)

    def test_declared_entry_nodes_win(self) -> None:
        wf = Workflow(
            name="w",
            nodes=(
                WorkflowNode(id="a", kind=NodeKind.TERMINAL),
                WorkflowNode(id="b", kind=NodeKind.TERMINAL),
            ),
            entry_nodes=("b",),
        )
        assert wf.resolved_entry_nodes == ("b",)

    def test_accessors(self, diamond_workflow: Workflow) -> None:
        assert diamond_workflow.node("join") is not None
        assert diamond_workflow.node("missing") is None
        assert {e.target for e in diamond_workflow.successors("a")} == {"b", "c"}
        assert {e.source for e in diamond_workflow.predecessors("join")} == {"b", "c"}
        assert diamond_workflow.agent_ids == frozenset(
            {"research_agent", "pricing_agent", "feature_agent", "analyst_agent"}
        )

    def test_extended_with_returns_a_new_graph(self, linear_workflow: Workflow) -> None:
        """Replanning must not mutate the graph already captured in checkpoints."""
        extended = linear_workflow.extended_with(
            nodes=(WorkflowNode(id="extra", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            edges=(WorkflowEdge(source="b", target="extra"),),
        )
        assert len(linear_workflow.nodes) == 3, "original graph was mutated"
        assert len(extended.nodes) == 4
        assert extended.dynamic is True

    def test_extension_still_validates(self, linear_workflow: Workflow) -> None:
        with pytest.raises(ValidationError):
            linear_workflow.extended_with(
                nodes=(WorkflowNode(id="extra", kind=NodeKind.TERMINAL),),
                edges=(WorkflowEdge(source="extra", target="ghost"),),
            )

    def test_mermaid_renders_every_node_and_edge(self, diamond_workflow: Workflow) -> None:
        diagram = diamond_workflow.to_mermaid()
        assert diagram.startswith("flowchart TD")
        for node in diamond_workflow.nodes:
            assert node.id in diagram
        assert diagram.count("-->") >= len(diamond_workflow.edges)

    def test_mermaid_labels_conditional_edges(self, conditional_workflow: Workflow) -> None:
        diagram = conditional_workflow.to_mermaid()
        assert "low confidence" in diagram


class TestConditions:
    def test_condition_describes_itself(self) -> None:
        cond = Condition(path="outputs.a.confidence", operator="gte", value=0.7)
        assert cond.describe() == "outputs.a.confidence gte 0.7"

    def test_unknown_operator_is_rejected(self) -> None:
        """The operator set is closed so conditions cannot become code."""
        with pytest.raises(ValidationError):
            Condition(path="a", operator="exec", value=1)  # type: ignore[arg-type]

    def test_empty_condition_group_means_always(self) -> None:
        group = NodeCondition()
        assert group.is_empty is True
        assert group.describe() == "always"

    def test_group_describes_its_mode(self) -> None:
        group = NodeCondition(
            conditions=(
                Condition(path="a", operator="truthy"),
                Condition(path="b", operator="falsy"),
            ),
            mode="any",
        )
        assert " OR " in group.describe()

    def test_edge_reports_conditionality(self) -> None:
        plain = WorkflowEdge(source="a", target="b")
        assert plain.is_conditional is False
        conditional = WorkflowEdge(
            source="a",
            target="b",
            condition=NodeCondition(conditions=(Condition(path="x", operator="truthy"),)),
        )
        assert conditional.is_conditional is True
        assert "[" in conditional.describe()

    def test_edge_with_empty_condition_group_is_not_conditional(self) -> None:
        edge = WorkflowEdge(source="a", target="b", condition=NodeCondition())
        assert edge.is_conditional is False


class TestRoutingDecisionSchema:
    """The supervisor contract. Every rejection here is a hallucination stopped."""

    def test_valid_parallel_delegate(self) -> None:
        decision = RoutingDecision(
            action=SupervisorAction.PARALLEL_DELEGATE,
            reason="three independent research dimensions",
            confidence=0.91,
            targets=(
                DelegationTarget(agent_id="research_agent", instruction="find vendors"),
                DelegationTarget(agent_id="pricing_agent", instruction="find pricing"),
            ),
        )
        assert decision.is_parallel is True
        assert decision.requires_agents is True
        assert decision.is_terminal is False
        assert decision.agent_ids == ("research_agent", "pricing_agent")

    def test_delegate_requires_exactly_one_target(self) -> None:
        with pytest.raises(ValidationError, match="exactly 1 target"):
            RoutingDecision(
                action=SupervisorAction.DELEGATE,
                reason="r",
                targets=(
                    DelegationTarget(agent_id="a", instruction="i"),
                    DelegationTarget(agent_id="b", instruction="i"),
                ),
            )

    def test_parallel_delegate_requires_at_least_two(self) -> None:
        with pytest.raises(ValidationError, match="at least 2 targets"):
            RoutingDecision(
                action=SupervisorAction.PARALLEL_DELEGATE,
                reason="r",
                targets=(DelegationTarget(agent_id="a", instruction="i"),),
            )

    @pytest.mark.parametrize(
        "action", [SupervisorAction.RESPOND_DIRECTLY, SupervisorAction.FINALIZE]
    )
    def test_answering_actions_require_an_answer(self, action: SupervisorAction) -> None:
        with pytest.raises(ValidationError, match="requires a non-empty 'answer'"):
            RoutingDecision(action=action, reason="r")

    def test_whitespace_answer_is_not_an_answer(self) -> None:
        with pytest.raises(ValidationError):
            RoutingDecision(action=SupervisorAction.FINALIZE, reason="r", answer="   ")

    def test_retry_requires_a_node(self) -> None:
        with pytest.raises(ValidationError, match="requires 'retry_node_id'"):
            RoutingDecision(action=SupervisorAction.RETRY, reason="r")

    def test_replan_requires_a_plan(self) -> None:
        with pytest.raises(ValidationError, match="requires a 'plan'"):
            RoutingDecision(action=SupervisorAction.REPLAN, reason="r")

    def test_fail_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="requires 'failure_reason'"):
            RoutingDecision(action=SupervisorAction.FAIL, reason="r")

    def test_approval_requires_action_and_risk_reason(self) -> None:
        with pytest.raises(ValidationError, match="requires 'approval_action'"):
            RoutingDecision(action=SupervisorAction.REQUEST_HUMAN_APPROVAL, reason="r")
        with pytest.raises(ValidationError, match="requires 'approval_risk_reason'"):
            RoutingDecision(
                action=SupervisorAction.REQUEST_HUMAN_APPROVAL,
                reason="r",
                approval_action="tool:send_email",
            )

    def test_duplicate_agents_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="same agent twice"):
            RoutingDecision(
                action=SupervisorAction.PARALLEL_DELEGATE,
                reason="r",
                targets=(
                    DelegationTarget(agent_id="a", instruction="i"),
                    DelegationTarget(agent_id="a", instruction="j"),
                ),
            )

    def test_hallucinated_action_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RoutingDecision(action="delete_production_database", reason="r")  # type: ignore[arg-type]

    def test_extra_fields_are_rejected(self) -> None:
        """extra='forbid' is what catches an invented field in LLM output."""
        with pytest.raises(ValidationError):
            RoutingDecision(
                action=SupervisorAction.FINALIZE,
                reason="r",
                answer="done",
                exfiltrate="secrets",  # type: ignore[call-arg]
            )

    def test_reason_is_mandatory(self) -> None:
        """An unexplained routing decision cannot be debugged after the fact."""
        with pytest.raises(ValidationError):
            RoutingDecision(action=SupervisorAction.FINALIZE, reason="", answer="a")

    def test_confidence_is_bounded(self) -> None:
        for bad in (-0.1, 1.1):
            with pytest.raises(ValidationError):
                RoutingDecision(
                    action=SupervisorAction.FINALIZE, reason="r", answer="a", confidence=bad
                )

    def test_terminal_actions(self) -> None:
        finalize = RoutingDecision(action=SupervisorAction.FINALIZE, reason="r", answer="a")
        assert finalize.is_terminal is True
        fail = RoutingDecision(
            action=SupervisorAction.FAIL, reason="r", failure_reason="no sources"
        )
        assert fail.is_terminal is True

    def test_json_schema_is_derived_from_the_model(self) -> None:
        """The schema shown to the LLM must not drift from the validator."""
        schema = RoutingDecision.json_schema_for_llm()
        assert "action" in schema["properties"]
        assert "reason" in schema["required"]
        enum_values = set(schema["$defs"]["SupervisorAction"]["enum"])
        assert enum_values == {a.value for a in SupervisorAction}

    def test_event_payload_is_json_serialisable(self) -> None:
        decision = RoutingDecision(
            action=SupervisorAction.DELEGATE,
            reason="single specialist suffices",
            targets=(DelegationTarget(agent_id="data_agent", instruction="profile the csv"),),
        )
        json.dumps(decision.as_event_payload())

    def test_parses_from_raw_llm_json(self) -> None:
        """The realistic path: a JSON string produced by a model."""
        raw = json.dumps(
            {
                "action": "parallel_delegate",
                "reason": "independent dimensions",
                "confidence": 0.88,
                "targets": [
                    {"agent_id": "research_agent", "instruction": "vendors"},
                    {"agent_id": "pricing_agent", "instruction": "pricing"},
                ],
            }
        )
        decision = RoutingDecision.model_validate_json(raw)
        assert decision.action is SupervisorAction.PARALLEL_DELEGATE
        assert len(decision.targets) == 2


class TestDynamicPlan:
    def test_plan_edges_must_resolve_within_the_plan(self) -> None:
        with pytest.raises(ValidationError, match="outside the plan"):
            DynamicPlan(
                nodes=(WorkflowNode(id="new", kind=NodeKind.TERMINAL),),
                edges=(WorkflowEdge(source="new", target="somewhere_else"),),
            )

    def test_attach_points_are_permitted_edge_endpoints(self) -> None:
        plan = DynamicPlan(
            nodes=(WorkflowNode(id="new", kind=NodeKind.AGENT, agent_id="critic_agent"),),
            edges=(WorkflowEdge(source="existing", target="new"),),
            attach_after=("existing",),
        )
        assert plan.attach_after == ("existing",)

    def test_a_plan_needs_at_least_one_node(self) -> None:
        with pytest.raises(ValidationError):
            DynamicPlan(nodes=())


class TestToolSpecSafety:
    def test_non_idempotent_tool_cannot_declare_retries(self) -> None:
        """Prevents a latent duplicate-send bug at definition time."""
        with pytest.raises(ValidationError, match="not idempotent"):
            ToolSpec(
                name="send_email",
                description="send mail",
                idempotent=False,
                retry_policy=RetryPolicy(max_attempts=3),
            )

    def test_non_idempotent_tool_with_no_retry_is_fine(self) -> None:
        spec = ToolSpec(
            name="send_email",
            description="send mail",
            idempotent=False,
            retry_policy=NO_RETRY_POLICY,
            risk=RiskLevel.HIGH,
            requires_approval=True,
        )
        assert spec.is_retryable is False

    def test_critical_tool_must_not_be_enabled_by_default(self) -> None:
        with pytest.raises(ValidationError, match="must not be enabled by default"):
            ToolSpec(
                name="exec_shell",
                description="run a shell command",
                risk=RiskLevel.CRITICAL,
                idempotent=False,
                retry_policy=NO_RETRY_POLICY,
            )

    def test_critical_tool_must_require_approval(self) -> None:
        with pytest.raises(ValidationError, match="must set requires_approval"):
            ToolSpec(
                name="exec_shell",
                description="run a shell command",
                risk=RiskLevel.CRITICAL,
                idempotent=False,
                retry_policy=NO_RETRY_POLICY,
                enabled_by_default=False,
            )

    def test_fully_gated_critical_tool_is_accepted(self) -> None:
        spec = ToolSpec(
            name="exec_shell",
            description="run a shell command",
            risk=RiskLevel.CRITICAL,
            idempotent=False,
            retry_policy=NO_RETRY_POLICY,
            enabled_by_default=False,
            requires_approval=True,
        )
        assert spec.risk is RiskLevel.CRITICAL

    def test_llm_schema_shape(self, calculator_tool: ToolSpec) -> None:
        schema = calculator_tool.to_llm_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "calculator"
        assert schema["function"]["parameters"]["required"] == ["expression"]

    def test_timeout_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            ToolSpec(name="t", description="d", timeout_seconds=10_000)


class TestToolResult:
    def test_success_factory(self) -> None:
        result = ToolResult.success("calculator", {"value": 4}, duration_seconds=0.01)
        assert result.ok is True
        assert result.output == {"value": 4}
        assert "ok" in result.as_llm_text()

    def test_failure_factory(self) -> None:
        result = ToolResult.failure(
            "web_search", error_code="timeout", error_message="upstream slow"
        )
        assert result.ok is False
        assert "timeout" in result.as_llm_text()

    def test_failures_are_values_not_exceptions(self) -> None:
        """An agent should be able to reason about a failed tool, not just die."""
        result = ToolResult.failure("web_search", error_code="e", error_message="m")
        assert isinstance(result, ToolResult)


class TestAgentDefinition:
    def test_duplicate_tool_permissions_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate tool permissions"):
            AgentDefinition(
                id="a",
                name="A",
                description="d",
                system_prompt="p",
                allowed_tools=(ToolPermission(tool="x"), ToolPermission(tool="x")),
            )

    def test_duplicate_capabilities_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate capabilities"):
            AgentDefinition(
                id="a",
                name="A",
                description="d",
                system_prompt="p",
                capabilities=(
                    AgentCapability(name="c", description="d"),
                    AgentCapability(name="c", description="d2"),
                ),
            )

    def test_system_prompt_is_required(self) -> None:
        with pytest.raises(ValidationError):
            AgentDefinition(id="a", name="A", description="d", system_prompt="")

    def test_allowlist_is_deny_by_default(self, research_agent: AgentDefinition) -> None:
        assert research_agent.may_attempt("web_search") is True
        assert research_agent.may_attempt("send_email") is False
        assert research_agent.may_attempt("exec_shell") is False

    def test_permission_lookup(self, research_agent: AgentDefinition) -> None:
        perm = research_agent.permission_for("read_file")
        assert perm is not None
        assert perm.constraints == {"path": {"prefix": "./data"}}
        assert research_agent.permission_for("nope") is None

    def test_capability_scoring_ranks_matches(self, research_agent: AgentDefinition) -> None:
        assert research_agent.capability_score("research and compare vendors") > 0
        assert research_agent.capability_score("unrelated topic") == 0.0

    def test_scoring_is_weighted_by_proficiency(self) -> None:
        strong = AgentDefinition(
            id="s",
            name="S",
            description="d",
            system_prompt="p",
            capabilities=(
                AgentCapability(
                    name="c", description="d", keywords=frozenset({"x"}), proficiency=1.0
                ),
            ),
        )
        weak = strong.model_copy(
            update={
                "capabilities": (
                    AgentCapability(
                        name="c", description="d", keywords=frozenset({"x"}), proficiency=0.2
                    ),
                )
            }
        )
        assert strong.capability_score("x") > weak.capability_score("x")

    def test_agent_with_no_capabilities_scores_zero(self) -> None:
        agent = AgentDefinition(id="a", name="A", description="d", system_prompt="p")
        assert agent.capability_score("anything") == 0.0

    def test_tool_use_capability_is_implied_by_having_tools(
        self, research_agent: AgentDefinition
    ) -> None:
        assert ModelCapability.TOOL_USE in research_agent.required_model_capabilities()

    def test_supervisor_summary_omits_internals(self, research_agent: AgentDefinition) -> None:
        """Keeping the summary small directly reduces routing token cost."""
        summary = research_agent.summary_for_supervisor()
        assert set(summary) == {"id", "name", "description", "capabilities", "tools"}
        assert "system_prompt" not in summary
        assert "config" not in summary


class TestAgentOutput:
    def test_low_confidence_signal(self) -> None:
        assert AgentOutput(content="x", confidence=0.3).is_low_confidence is True
        assert AgentOutput(content="x", confidence=0.7).is_low_confidence is False

    def test_unsupported_claims_detected_without_an_llm(self) -> None:
        bare = AgentOutput(content="x", claims=("a", "b"), evidence=())
        assert bare.unsupported_claim_count == 2
        supported = AgentOutput(content="x", claims=("a", "b"), evidence=("src",))
        assert supported.unsupported_claim_count == 0

    def test_context_text_includes_evidence_and_gaps(self) -> None:
        out = AgentOutput(
            content="body", evidence=("https://a", "https://b"), gaps=("missing price for X",)
        )
        text = out.as_context_text()
        assert "Evidence:" in text
        assert "Open gaps:" in text

    def test_context_text_truncates(self) -> None:
        out = AgentOutput(content="y" * 10_000)
        assert len(out.as_context_text(max_chars=100)) <= 120


class TestBudgetSchema:
    def test_limit_lookup_for_every_dimension(self) -> None:
        budget = Budget()
        for dim in BudgetDimension:
            assert budget.limit_for(dim) is not None

    def test_none_means_unmetered(self) -> None:
        budget = Budget(max_cost_usd=None)
        assert budget.limit_for(BudgetDimension.COST_USD) is None
        assert BudgetDimension.COST_USD not in budget.metered_dimensions

    def test_tightening_takes_the_stricter_of_each(self) -> None:
        """An agent budget may never widen what the execution allows."""
        loose = Budget(max_cost_usd=1.0, max_tokens=100_000, max_agent_steps=None)
        strict = Budget(max_cost_usd=0.1, max_tokens=None, max_agent_steps=5)
        merged = loose.tightened_to(strict)
        assert merged.max_cost_usd == 0.1
        assert merged.max_tokens == 100_000
        assert merged.max_agent_steps == 5

    def test_budget_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            Budget().max_tokens = 1  # type: ignore[misc]

    def test_rejects_non_positive_limits(self) -> None:
        with pytest.raises(ValidationError):
            Budget(max_cost_usd=0)

    def test_usage_accumulates_llm_calls(self) -> None:
        usage = BudgetUsage()
        usage.add_llm_usage(input_tokens=100, output_tokens=20, cost_usd=0.002)
        usage.add_llm_usage(input_tokens=50, output_tokens=10, cost_usd=0.001)
        assert usage.total_tokens == 180
        assert usage.cost_usd == 0.003
        assert usage.llm_calls == 2

    def test_usage_merge_folds_parallel_branches(self) -> None:
        a = BudgetUsage(cost_usd=0.01, input_tokens=10, agent_steps=1)
        b = BudgetUsage(cost_usd=0.02, output_tokens=5, agent_steps=2)
        merged = a.merge(b)
        assert merged.cost_usd == 0.03
        assert merged.agent_steps == 3
        assert merged.total_tokens == 15

    def test_duration_comes_from_the_caller_not_the_tally(self) -> None:
        """Usage holds no clock, so a resumed run cannot lose elapsed time."""
        usage = BudgetUsage()
        assert usage.value_for(BudgetDimension.DURATION_SECONDS, elapsed_seconds=42.0) == 42.0


class TestModelConfigSchema:
    def test_cost_is_computed_from_published_per_mtok_rates(self) -> None:
        model = ModelConfig(
            key="m",
            provider=Provider.OPENAI,
            model="m",
            input_cost_per_mtok=3.0,
            output_cost_per_mtok=15.0,
        )
        assert model.estimate_cost(1_000_000, 0) == 3.0
        assert model.estimate_cost(0, 1_000_000) == 15.0
        # 1000 in @ $3/Mtok = $0.003; 1000 out @ $15/Mtok = $0.015
        assert model.estimate_cost(1000, 1000) == 0.018

    def test_output_must_fit_inside_context(self) -> None:
        with pytest.raises(ValidationError, match="must be less than"):
            ModelConfig(
                key="m",
                provider=Provider.MOCK,
                model="m",
                context_limit=1_000,
                max_output_tokens=1_000,
            )

    def test_capability_query(self) -> None:
        model = ModelConfig(
            key="m",
            provider=Provider.MOCK,
            model="m",
            capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_USE}),
        )
        assert model.has(ModelCapability.CHAT) is True
        assert model.has(ModelCapability.CHAT, ModelCapability.TOOL_USE) is True
        assert model.has(ModelCapability.VISION) is False

    def test_free_models_are_identified(self) -> None:
        assert ModelConfig(key="m", provider=Provider.MOCK, model="m").is_free is True


class TestMessages:
    def test_constructors_set_the_role(self) -> None:
        assert Message.system("s").role.value == "system"
        assert Message.user("u").role.value == "user"
        assert Message.assistant("a").role.value == "assistant"

    def test_tool_result_carries_the_call_id(self) -> None:
        msg = Message.tool_result("42", tool_call_id="call_1", name="calculator")
        assert msg.tool_call_id == "call_1"
        assert msg.name == "calculator"

    def test_messages_are_immutable(self) -> None:
        with pytest.raises(ValidationError):
            Message.user("x").content = "y"  # type: ignore[misc]

    def test_token_usage_adds(self) -> None:
        total = TokenUsage(input_tokens=10, output_tokens=5) + TokenUsage(
            input_tokens=1, output_tokens=2
        )
        assert total.input_tokens == 11
        assert total.total_tokens == 18
