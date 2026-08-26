"""Tests for the agent registry and the reference agent definitions.

Two things are being defended here:

1. The registry supports genuine runtime registration -- register, get, list,
   update, remove -- because the supervisor queries it rather than hard-coding
   agents.
2. The reference agents' permission boundaries are what they claim to be. These
   are asserted explicitly, since "the research agent cannot send email" is a
   security property, not a doc comment.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from orchestration.agents.definitions import (
    ANALYST_AGENT,
    CODE_AGENT,
    CRITIC_AGENT,
    DATA_AGENT,
    FEATURE_AGENT,
    FINALIZER_AGENT,
    PRICING_AGENT,
    REFERENCE_AGENTS,
    RESEARCH_AGENT,
    build_default_agent_registry,
)
from orchestration.agents.registry import AgentRegistry
from orchestration.domain.agent import AgentDefinition
from orchestration.domain.tool import AgentCapability
from orchestration.errors import DuplicateError, NotFoundError
from orchestration.tools.registry import build_default_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> AgentRegistry:
    return build_default_agent_registry()


def _agent(agent_id: str, **kwargs: object) -> AgentDefinition:
    defaults: dict[str, object] = {
        "id": agent_id,
        "name": agent_id.title(),
        "description": "a test agent",
        "system_prompt": "do the thing",
    }
    return AgentDefinition.model_validate({**defaults, **kwargs})


class TestRegistration:
    def test_register_and_get(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha"))
        assert registry.get("alpha").id == "alpha"
        assert "alpha" in registry
        assert len(registry) == 1

    def test_duplicate_is_rejected(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha"))
        with pytest.raises(DuplicateError, match="already registered"):
            registry.register(_agent("alpha"))

    def test_replace_is_explicit(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha", description="first"))
        registry.register(_agent("alpha", description="second"), replace=True)
        assert registry.get("alpha").description == "second"

    def test_unknown_agent_lists_alternatives(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("research-agent"))
        with pytest.raises(NotFoundError) as info:
            registry.get("reserach-agent")
        assert "research-agent" in info.value.context["available"]

    def test_try_get_returns_none(self) -> None:
        assert AgentRegistry().try_get("nope") is None

    def test_remove(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha"))
        registry.remove("alpha")
        assert "alpha" not in registry
        with pytest.raises(NotFoundError):
            registry.remove("alpha")

    async def test_concurrent_registration_is_safe(self) -> None:
        registry = AgentRegistry()
        await asyncio.gather(*(registry.register_async(_agent(f"a{i}")) for i in range(25)))
        assert len(registry) == 25


class TestUpdate:
    def test_partial_update_revalidates(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha"))
        updated = registry.update("alpha", description="new description")
        assert updated.description == "new description"
        assert registry.get("alpha").description == "new description"

    def test_invalid_update_is_rejected_not_stored(self) -> None:
        """A bad update must not produce a definition that fails later at runtime."""
        registry = AgentRegistry()
        registry.register(_agent("alpha"))
        with pytest.raises(ValidationError):
            registry.update("alpha", max_iterations=999)
        assert registry.get("alpha").max_iterations == 6

    def test_update_bumps_the_timestamp(self) -> None:
        registry = AgentRegistry()
        original = registry.register(_agent("alpha"))
        updated = registry.update("alpha", description="changed")
        assert updated.updated_at >= original.updated_at

    def test_update_unknown_agent_raises(self) -> None:
        with pytest.raises(NotFoundError):
            AgentRegistry().update("ghost", description="x")


class TestListing:
    def test_disabled_agents_are_hidden_by_default(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("on"))
        registry.register(_agent("off", enabled=False))
        assert registry.ids() == ("on",)
        assert set(registry.ids(include_disabled=True)) == {"on", "off"}

    def test_listing_is_sorted_for_stable_output(self) -> None:
        registry = AgentRegistry()
        for name in ("zulu", "alpha", "mike"):
            registry.register(_agent(name))
        assert registry.ids() == ("alpha", "mike", "zulu")

    def test_by_kind(self, registry: AgentRegistry) -> None:
        research = registry.by_kind("research")
        assert {a.id for a in research} == {"research_agent", "pricing_agent", "feature_agent"}

    def test_by_capability(self, registry: AgentRegistry) -> None:
        assert any(a.id == "data_agent" for a in registry.by_capability("statistics"))

    def test_with_tool(self, registry: AgentRegistry) -> None:
        can_exec = {a.id for a in registry.with_tool("python_exec")}
        assert can_exec == {"data_agent", "code_agent"}


class TestCandidateShortlisting:
    """Deterministic ranking, used both as a prompt pre-filter and as fallback."""

    def test_ranks_a_research_task_to_research_agents(self, registry: AgentRegistry) -> None:
        candidates = registry.candidates_for("research and compare CRM vendors in the market")
        assert candidates
        assert candidates[0][0].kind == "research"

    def test_ranks_a_pricing_task_to_the_pricing_agent(self, registry: AgentRegistry) -> None:
        candidates = registry.candidates_for("find the price per seat of each subscription tier")
        assert candidates[0][0].id == "pricing_agent"

    def test_ranks_a_data_task_to_the_data_agent(self, registry: AgentRegistry) -> None:
        candidates = registry.candidates_for(
            "profile this csv dataset and compute the median of each column"
        )
        assert candidates[0][0].id == "data_agent"

    def test_ranks_a_code_task_to_the_code_agent(self, registry: AgentRegistry) -> None:
        candidates = registry.candidates_for(
            "find the failing pytest in this repository and read the module"
        )
        assert candidates[0][0].id == "code_agent"

    def test_ranks_a_critique_task_to_the_critic(self, registry: AgentRegistry) -> None:
        candidates = registry.candidates_for("audit this report for unsupported claims")
        assert candidates[0][0].id == "critic_agent"

    def test_unrelated_task_yields_no_candidates(self, registry: AgentRegistry) -> None:
        """No match must be distinguishable from a weak match."""
        assert registry.candidates_for("qwertyuiop zxcvbnm") == ()

    def test_ordering_is_stable_across_calls(self, registry: AgentRegistry) -> None:
        """The benchmark depends on identical shortlists across runs."""
        task = "compare pricing and features of vendors"
        first = [a.id for a, _ in registry.candidates_for(task, limit=5)]
        second = [a.id for a, _ in registry.candidates_for(task, limit=5)]
        assert first == second

    def test_ties_break_on_id_not_insertion_order(self) -> None:
        registry = AgentRegistry()
        capability = AgentCapability(
            name="c", description="d", keywords=frozenset({"widget"}), proficiency=0.8
        )
        for name in ("zeta", "alpha"):
            registry.register(_agent(name, capabilities=(capability.model_dump(),)))
        ranked = [a.id for a, _ in registry.candidates_for("widget")]
        assert ranked == ["alpha", "zeta"]

    def test_limit_is_respected(self, registry: AgentRegistry) -> None:
        assert len(registry.candidates_for("research compare find sources", limit=2)) <= 2

    def test_disabled_agents_are_never_candidates(self, registry: AgentRegistry) -> None:
        registry.update("pricing_agent", enabled=False)
        ids = [a.id for a, _ in registry.candidates_for("find the price per seat tier")]
        assert "pricing_agent" not in ids

    def test_best_for_returns_the_top_agent(self, registry: AgentRegistry) -> None:
        best = registry.best_for("summarise these sources with citations")
        assert best is not None
        assert best.kind == "research"

    def test_best_for_returns_none_on_no_match(self, registry: AgentRegistry) -> None:
        assert registry.best_for("qwertyuiop") is None


class TestSupervisorSummaries:
    def test_summaries_omit_prompts_and_config(self, registry: AgentRegistry) -> None:
        """Keeping the routing prompt small is a direct cost saving."""
        for summary in registry.summaries_for_supervisor():
            assert set(summary) == {"id", "name", "description", "capabilities", "tools"}

    def test_summaries_can_be_restricted_to_a_shortlist(self, registry: AgentRegistry) -> None:
        summaries = registry.summaries_for_supervisor(only=["research_agent", "critic_agent"])
        assert {s["id"] for s in summaries} == {"research_agent", "critic_agent"}


class TestToolCrossValidation:
    def test_reference_agents_only_reference_real_tools(self, registry: AgentRegistry) -> None:
        """Catches an agent definition drifting away from the tool registry."""
        tools = build_default_registry()
        assert registry.validate_against_tools(tools) == ()

    def test_reports_an_unknown_tool(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha", allowed_tools=({"tool": "nonexistent_tool"},)))
        problems = registry.validate_against_tools(build_default_registry())
        assert len(problems) == 1
        assert "nonexistent_tool" in problems[0]

    def test_reports_a_disabled_tool_distinctly(self) -> None:
        registry = AgentRegistry()
        registry.register(_agent("alpha", allowed_tools=({"tool": "python_exec"},)))
        problems = registry.validate_against_tools(build_default_registry(enable_python=False))
        assert len(problems) == 1
        assert "disabled" in problems[0]


class TestReferenceAgentPermissionBoundaries:
    """These assertions are the permission requirement, stated as tests."""

    def test_all_reference_agents_are_registered(self, registry: AgentRegistry) -> None:
        assert set(registry.ids()) == {a.id for a in REFERENCE_AGENTS}
        assert len(REFERENCE_AGENTS) == 8

    @pytest.mark.parametrize(
        "agent",
        [
            RESEARCH_AGENT,
            PRICING_AGENT,
            FEATURE_AGENT,
            DATA_AGENT,
            CODE_AGENT,
            ANALYST_AGENT,
            CRITIC_AGENT,
            FINALIZER_AGENT,
        ],
    )
    def test_no_reference_agent_may_execute_shell(self, agent: AgentDefinition) -> None:
        """The single most important negative permission in the system."""
        assert agent.may_attempt("exec_shell") is False

    @pytest.mark.parametrize(
        "agent",
        [RESEARCH_AGENT, PRICING_AGENT, FEATURE_AGENT, DATA_AGENT, CODE_AGENT, ANALYST_AGENT],
    )
    def test_no_worker_may_send_email(self, agent: AgentDefinition) -> None:
        assert agent.may_attempt("send_email") is False

    def test_research_agent_can_search_and_read_only(self) -> None:
        assert RESEARCH_AGENT.tool_names == {"web_search", "read_file", "http_request"}
        assert RESEARCH_AGENT.may_attempt("write_file") is False
        assert RESEARCH_AGENT.may_attempt("python_exec") is False
        assert RESEARCH_AGENT.may_attempt("db_query") is False

    def test_code_agent_has_no_database_or_shell_access(self) -> None:
        assert CODE_AGENT.may_attempt("db_query") is False
        assert CODE_AGENT.may_attempt("exec_shell") is False
        assert CODE_AGENT.may_attempt("python_exec") is True

    def test_code_agent_writes_are_confined_to_scratch(self) -> None:
        """A code inspector must not be able to modify the source it inspects."""
        permission = CODE_AGENT.permission_for("write_file")
        assert permission is not None
        assert permission.constraints == {"path": {"prefix": "scratch"}}

    def test_data_agent_has_no_network_access(self) -> None:
        """It is handed a dataset; it has no reason to reach the network."""
        assert DATA_AGENT.may_attempt("http_request") is False
        assert DATA_AGENT.may_attempt("web_search") is False

    def test_data_agent_writes_are_confined_to_the_analysis_directory(self) -> None:
        permission = DATA_AGENT.permission_for("write_file")
        assert permission is not None
        assert permission.constraints == {"path": {"prefix": "analysis"}}

    def test_synthesis_agents_hold_almost_no_tools(self) -> None:
        assert ANALYST_AGENT.tool_names == {"calculator"}
        assert CRITIC_AGENT.tool_names == {"web_search"}
        assert FINALIZER_AGENT.tool_names == {"write_file"}

    def test_finalizer_writes_only_into_reports(self) -> None:
        permission = FINALIZER_AGENT.permission_for("write_file")
        assert permission is not None
        assert permission.constraints == {"path": {"prefix": "reports"}}

    def test_call_ceilings_are_set_on_expensive_tools(self) -> None:
        """Bounds an agent's tool spend independently of the token budget."""
        permission = RESEARCH_AGENT.permission_for("web_search")
        assert permission is not None
        assert permission.max_calls == 12


class TestDerivedAgents:
    def test_derived_agents_share_the_research_tooling(self) -> None:
        assert PRICING_AGENT.tool_names == RESEARCH_AGENT.tool_names
        assert FEATURE_AGENT.tool_names == RESEARCH_AGENT.tool_names

    def test_derivation_did_not_mutate_the_original(self) -> None:
        """`merged` must copy, not mutate: these are module-level singletons."""
        assert RESEARCH_AGENT.id == "research_agent"
        assert "Your specific focus:" not in RESEARCH_AGENT.system_prompt
        assert not any(c.name.endswith("_focus") for c in RESEARCH_AGENT.capabilities)
        assert "derived" not in RESEARCH_AGENT.tags

    def test_derived_agents_carry_a_focus_capability(self) -> None:
        assert any(c.name == "pricing_agent_focus" for c in PRICING_AGENT.capabilities)

    def test_derived_agents_outrank_the_generic_one_on_their_topic(
        self, registry: AgentRegistry
    ) -> None:
        """This is what makes the parallel fan-out route to three distinct agents."""
        pricing = registry.candidates_for("pricing per seat licence cost")
        assert pricing[0][0].id == "pricing_agent"
        features = registry.candidates_for("ai capability feature integration")
        assert features[0][0].id == "feature_agent"

    def test_derived_agents_are_marked_as_derived(self) -> None:
        assert "derived" in PRICING_AGENT.tags


class TestReferenceAgentConsistency:
    @pytest.mark.parametrize("agent", REFERENCE_AGENTS, ids=lambda a: a.id)
    def test_every_agent_declares_capabilities(self, agent: AgentDefinition) -> None:
        """Without capabilities an agent can never be shortlisted."""
        assert agent.capabilities, f"{agent.id} has no capabilities"

    @pytest.mark.parametrize("agent", REFERENCE_AGENTS, ids=lambda a: a.id)
    def test_every_prompt_states_the_output_contract(self, agent: AgentDefinition) -> None:
        """The runtime parses one shape from every agent, so all must request it."""
        assert '"confidence"' in agent.system_prompt
        assert '"evidence"' in agent.system_prompt
        assert '"gaps"' in agent.system_prompt

    @pytest.mark.parametrize("agent", REFERENCE_AGENTS, ids=lambda a: a.id)
    def test_every_prompt_forbids_inventing_sources(self, agent: AgentDefinition) -> None:
        assert "Never invent a source" in agent.system_prompt

    @pytest.mark.parametrize("agent", REFERENCE_AGENTS, ids=lambda a: a.id)
    def test_iteration_and_timeout_bounds_are_set(self, agent: AgentDefinition) -> None:
        """A confused agent must not be able to spin until the budget dies."""
        assert 1 <= agent.max_iterations <= 10
        assert 0 < agent.timeout_seconds <= 600

    @pytest.mark.parametrize("agent", REFERENCE_AGENTS, ids=lambda a: a.id)
    def test_agents_with_tools_require_tool_use_capability(self, agent: AgentDefinition) -> None:
        from orchestration.domain.enums import ModelCapability

        if agent.allowed_tools:
            assert ModelCapability.TOOL_USE in agent.required_model_capabilities()

    def test_agent_ids_are_unique(self) -> None:
        ids = [a.id for a in REFERENCE_AGENTS]
        assert len(ids) == len(set(ids))
