"""The supervisor.

Turns execution state into a validated :class:`RoutingDecision`. Three layers,
each of which can reject what the previous one produced:

1. **Schema validation.** The model is asked for JSON matching
   :class:`RoutingDecision`; a reply that does not validate gets one repair
   attempt with its own errors fed back.
2. **Semantic validation.** A schema-valid decision can still be nonsense --
   naming an agent that does not exist, retrying a node that never ran, or
   delegating to a disabled agent. :meth:`Supervisor.validate_decision` catches
   that class of error, which schema validation structurally cannot.
3. **Fallback.** If no valid decision can be obtained, the deterministic
   :class:`HeuristicRouter` answers instead, and the outcome is marked degraded.

The engine therefore never dispatches on a decision that has not passed all
three. A hallucinated agent name fails at layer 2, before any work is scheduled.
"""

from __future__ import annotations

import time

from orchestration.agents.registry import AgentRegistry
from orchestration.domain.budget import BudgetSnapshot
from orchestration.domain.enums import (
    NodeKind,
    NodeStatus,
    SupervisorAction,
    TaskComplexity,
)
from orchestration.domain.execution import ExecutionState
from orchestration.domain.model import LLMRequest, Message, RoutingCriteria
from orchestration.domain.retry import RetryPolicy
from orchestration.domain.routing import (
    RoutingAttempt,
    RoutingDecision,
    RoutingOutcome,
)
from orchestration.domain.tool import ToolSpec
from orchestration.domain.workflow import DynamicPlan, Workflow
from orchestration.errors import (
    InputValidationError,
    OrchestrationError,
    SchemaViolationError,
)
from orchestration.llm.factory import LLMClient
from orchestration.routing.model_router import ModelRouter
from orchestration.supervisor.heuristic import HeuristicRouter
from orchestration.supervisor.prompt import build_supervisor_messages
from orchestration.tools.registry import ToolRegistry

#: Cap on supervisor-driven replans, so a model that keeps replanning instead of
#: working cannot spin the execution. Replanning is legitimate; replanning
#: forever is a failure mode.
MAX_REPLANS = 3


class Supervisor:
    """Produces validated routing decisions.

    Args:
        agents: Registry the supervisor may delegate to. It queries this rather
            than holding a hard-coded agent list.
        llm: Client used for the routing call.
        router: Model router; the supervisor asks for a reasoning-capable model.
        tools: Tool registry, used only to describe the system in the prompt.
        heuristic: Fallback router. Constructed from ``agents`` when omitted.
        shortlist_size: How many candidate agents to describe in the prompt.
            ``0`` describes all of them. Shortlisting keeps the routing prompt
            small on a large registry, which is a direct per-step cost saving.
        max_repairs: Schema-repair attempts before falling back.
    """

    def __init__(
        self,
        *,
        agents: AgentRegistry,
        llm: LLMClient,
        router: ModelRouter,
        tools: ToolRegistry | None = None,
        heuristic: HeuristicRouter | None = None,
        shortlist_size: int = 0,
        max_repairs: int = 1,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._agents = agents
        self._llm = llm
        self._router = router
        self._tools = tools
        self._heuristic = heuristic or HeuristicRouter(agents)
        self._shortlist_size = shortlist_size
        self._max_repairs = max_repairs
        self._retry_policy = retry_policy

    # -- main entry point --------------------------------------------------

    async def decide(
        self,
        state: ExecutionState,
        *,
        budget: BudgetSnapshot | None = None,
        extra_instruction: str | None = None,
        workflow: Workflow | None = None,
    ) -> RoutingOutcome:
        """Obtain the next validated routing decision.

        Never raises for a routing failure: an unusable model degrades to the
        heuristic router and the outcome records that. The engine can then
        proceed, and the degradation is visible in events and benchmarks rather
        than being silently smoothed over.
        """
        attempts: list[RoutingAttempt] = []
        agent_summaries = self._agent_summaries(state)
        tool_specs = self._tool_specs()

        system_prompt, user_prompt = build_supervisor_messages(
            state=state,
            agent_summaries=agent_summaries,
            tool_specs=tool_specs,
            budget=budget,
            extra_instruction=extra_instruction,
        )

        selection = self._router.select(
            RoutingCriteria(prefer="most_capable"), complexity=TaskComplexity.COMPLEX
        )
        request = LLMRequest(
            messages=(Message.system(system_prompt), Message.user(user_prompt)),
            model=selection.model,
            request_key=f"supervisor:{state.execution_id}:{len(state.node_states)}",
        )

        started = time.perf_counter()
        try:
            decision, structured = await self._llm.complete_structured(
                request,
                RoutingDecision,
                max_repairs=self._max_repairs,
                retry_policy=self._retry_policy,
            )
        except (SchemaViolationError, OrchestrationError) as exc:
            attempts.append(
                RoutingAttempt(
                    attempt=1,
                    valid=False,
                    validation_errors=(str(exc),),
                    model_key=selection.model.key,
                    latency_seconds=round(time.perf_counter() - started, 6),
                )
            )
            return self._degrade(
                state, attempts, reason=f"model could not produce a valid decision: {exc}"
            )

        attempts.append(
            RoutingAttempt(
                attempt=structured.attempts,
                raw_output=structured.raw_outputs[-1] if structured.raw_outputs else "",
                valid=True,
                validation_errors=structured.validation_errors,
                decision=decision,
                model_key=selection.model.key,
                input_tokens=structured.total_input_tokens,
                output_tokens=structured.total_output_tokens,
                cost_usd=structured.total_cost_usd,
                latency_seconds=structured.latency_seconds,
            )
        )

        # Layer 2: semantic validation. Schema-valid but wrong is still wrong.
        problems = self.validate_decision(decision, state, workflow=workflow)
        if problems:
            return self._degrade(
                state,
                attempts,
                reason="decision failed semantic validation: " + "; ".join(problems),
            )

        return RoutingOutcome(
            decision=decision,
            attempts=tuple(attempts),
            degraded=structured.repaired,
        )

    # -- semantic validation ------------------------------------------------

    def validate_decision(
        self,
        decision: RoutingDecision,
        state: ExecutionState,
        *,
        workflow: Workflow | None = None,
    ) -> list[str]:
        """Check a schema-valid decision against reality.

        Returns a list of problems rather than raising, so every issue is
        reported at once and the caller decides whether to degrade or fail.

        This is the layer that catches what a JSON schema cannot: agent names
        that do not exist, nodes that were never scheduled, replans that exceed
        the churn limit, and plans referencing unknown agents or tools.
        """
        problems: list[str] = []

        for target in decision.targets:
            definition = self._agents.try_get(target.agent_id)
            if definition is None:
                problems.append(
                    f"unknown agent {target.agent_id!r} "
                    f"(known: {', '.join(self._agents.ids()) or 'none'})"
                )
            elif not definition.enabled:
                problems.append(f"agent {target.agent_id!r} is disabled")

        if decision.action is SupervisorAction.RETRY:
            problems.extend(self._validate_retry(decision, state))

        if decision.action is SupervisorAction.REPLAN and decision.plan is not None:
            problems.extend(self._validate_plan(decision.plan, state, workflow))

        if decision.action is SupervisorAction.PARALLEL_DELEGATE and len(decision.targets) > 8:
            # Not a schema limit because the sensible ceiling is deployment
            # specific, but an unbounded fan-out will exhaust the budget in one
            # step regardless of configuration.
            problems.append(
                f"parallel_delegate requests {len(decision.targets)} agents, "
                "which will exhaust the budget in a single step"
            )

        return problems

    def _validate_retry(self, decision: RoutingDecision, state: ExecutionState) -> list[str]:
        node_id = decision.retry_node_id
        assert node_id is not None  # guaranteed by the schema validator
        node_state = state.node_states.get(node_id)
        if node_state is None:
            return [f"cannot retry node {node_id!r}: it has never run"]
        if node_state.status is NodeStatus.SUCCEEDED:
            return [f"cannot retry node {node_id!r}: it already succeeded"]
        error = node_state.error or {}
        if error.get("retryable") is False:
            return [
                f"cannot retry node {node_id!r}: its failure "
                f"({error.get('code', 'unknown')}) is terminal"
            ]
        return []

    def _validate_plan(
        self, plan: DynamicPlan, state: ExecutionState, workflow: Workflow | None
    ) -> list[str]:
        """Validate a dynamically generated subgraph before it becomes executable.

        A plan the supervisor invented is untrusted input in exactly the same way
        a user-submitted workflow is, and gets the same scrutiny.
        """
        problems: list[str] = []

        if state.replan_count >= MAX_REPLANS:
            problems.append(
                f"replan limit reached ({state.replan_count}/{MAX_REPLANS}); "
                "the supervisor must make progress or finalize"
            )

        known_agents = set(self._agents.ids())
        known_tools = set(self._tools.names()) if self._tools else set()
        existing_nodes = {n.id for n in workflow.nodes} if workflow else set(state.node_states)

        for node in plan.nodes:
            if node.id in existing_nodes:
                problems.append(f"plan node {node.id!r} collides with an existing node")
            if node.kind is NodeKind.AGENT and node.agent_id not in known_agents:
                problems.append(f"plan node {node.id!r} names unknown agent {node.agent_id!r}")
            if node.kind is NodeKind.TOOL and self._tools and node.tool not in known_tools:
                problems.append(f"plan node {node.id!r} names unknown tool {node.tool!r}")

        for attach in plan.attach_after:
            if attach not in existing_nodes:
                problems.append(
                    f"plan attaches after unknown node {attach!r} "
                    f"(existing: {', '.join(sorted(existing_nodes)) or 'none'})"
                )

        return problems

    # -- plan compilation ---------------------------------------------------

    def compile_plan(self, plan: DynamicPlan, workflow: Workflow) -> Workflow:
        """Merge a validated plan into the live workflow.

        Returns a new :class:`Workflow`; the pre-replan graph stays intact in the
        checkpoint history so a resume can reconstruct either topology.

        Raises:
            InputValidationError: If the merged graph fails validation. Checked
                here as a final guard -- an invalid graph must never reach the
                scheduler, even if semantic validation somehow passed it.
        """
        from orchestration.workflow.graph import WorkflowGraph

        edges = list(plan.edges)
        attached = {e.target for e in plan.edges}
        if plan.attach_after:
            from orchestration.domain.workflow import WorkflowEdge

            for node in plan.nodes:
                if node.id in attached:
                    continue
                # A plan node with no inbound edge would be unreachable, so wire
                # it to the declared attachment points.
                edges.extend(
                    WorkflowEdge(source=source, target=node.id) for source in plan.attach_after
                )

        try:
            merged = workflow.extended_with(nodes=plan.nodes, edges=tuple(edges))
        except Exception as exc:
            raise InputValidationError(
                "supervisor plan produced an invalid workflow",
                detail=str(exc),
            ) from exc

        problems = WorkflowGraph(merged).validate()
        if problems:
            raise InputValidationError(
                "supervisor plan produced a structurally invalid graph",
                problems=problems,
            )
        return merged

    # -- helpers -----------------------------------------------------------

    def _agent_summaries(self, state: ExecutionState) -> tuple[dict[str, object], ...]:
        """Agents to describe in the prompt, shortlisted when configured."""
        if self._shortlist_size <= 0:
            return self._agents.summaries_for_supervisor()
        candidates = self._agents.candidates_for(state.task.description, limit=self._shortlist_size)
        if not candidates:
            # An empty shortlist would leave the supervisor unable to delegate at
            # all, which is worse than a larger prompt.
            return self._agents.summaries_for_supervisor()
        return self._agents.summaries_for_supervisor(only=[a.id for a, _ in candidates])

    def _tool_specs(self) -> tuple[ToolSpec, ...]:
        if self._tools is None:
            return ()
        return self._tools.list_specs()

    def _degrade(
        self, state: ExecutionState, attempts: list[RoutingAttempt], *, reason: str
    ) -> RoutingOutcome:
        """Fall back to the heuristic router and record the degradation."""
        decision = self._heuristic.decide(state, reason_prefix="[fallback]")
        attempts.append(
            RoutingAttempt(
                attempt=len(attempts) + 1,
                valid=True,
                decision=decision,
                from_fallback=True,
                validation_errors=(reason,),
            )
        )
        return RoutingOutcome(decision=decision, attempts=tuple(attempts), degraded=True)
