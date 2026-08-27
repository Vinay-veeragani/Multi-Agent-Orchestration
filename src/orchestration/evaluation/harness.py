"""Runs one scenario under one arm and grades the result.

:func:`run_scenario` assembles a complete, fresh engine per call -- registries,
policy engine, a scripted :class:`MockProvider`, budget meter -- runs it, and
hands the outcome to :func:`~orchestration.evaluation.judge.judge`. Fresh per
call rather than shared across scenarios so one scenario's registrations or
policy call-counts can never leak into the next.

Every scenario in this benchmark is workflow-less (``workflow_ref`` is
unused): the four arms are an ablation over the *dynamic* orchestrator's own
capabilities (routing, retry, parallelism), and a hand-authored static
workflow would hold the graph fixed regardless of arm, which would not
exercise the thing being compared.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import cast

from orchestration.agents.definitions import build_default_agent_registry
from orchestration.agents.registry import AgentRegistry
from orchestration.agents.runtime import AgentRunContext, AgentRuntime
from orchestration.budget.meter import BudgetGuard, BudgetMeter
from orchestration.coordination.redis import RedisCoordinator
from orchestration.domain.base import JsonDict, new_id
from orchestration.domain.budget import UNLIMITED_BUDGET, Budget, BudgetUsage
from orchestration.domain.enums import (
    ExecutionStatus,
    NodeKind,
    PolicyEffect,
    SupervisorAction,
)
from orchestration.domain.evaluation import BenchmarkScenario, ScenarioResult
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolResult
from orchestration.domain.workflow import Task, Workflow, WorkflowNode
from orchestration.errors import BudgetExceededError
from orchestration.evaluation.arms import Arm
from orchestration.evaluation.judge import judge
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.llm.factory import LLMClient
from orchestration.llm.mock import Fault, FaultKind, MockProvider, MockScript
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import ExecutionRepository, WorkflowRepository
from orchestration.policies.approvals import ApprovalService
from orchestration.policies.engine import PolicyEngine, build_default_policy_engine
from orchestration.routing.model_router import ModelRouter, build_default_router
from orchestration.runtime.orchestrator import ExecutionOrchestrator, seed_dynamic_workflow
from orchestration.supervisor.heuristic import HeuristicRouter
from orchestration.supervisor.supervisor import Supervisor
from orchestration.tools.registry import ToolRegistry, build_default_registry


async def _no_sleep(delay: float) -> None:
    return None


def _fault_from(entry: JsonDict) -> Fault:
    kind = cast("FaultKind", entry.get("error", "timeout"))
    attempts = tuple(entry.get("attempts", (1,)))
    return Fault(kind=kind, attempts=attempts)


def _build_provider(scenario: BenchmarkScenario) -> MockProvider:
    """Translate a scenario's ``mock_script``/``fault_injection`` into a provider."""
    script = MockScript()
    keys = set(scenario.mock_script) | set(scenario.fault_injection)
    for key in sorted(keys):
        responses = tuple(scenario.mock_script.get(key, ()))
        fault_entry = scenario.fault_injection.get(key)
        fault = _fault_from(fault_entry) if fault_entry else None
        if key == "supervisor":
            script.on_supervisor(*responses, fault=fault)
        else:
            script.on_agent_id(key, *responses, fault=fault)
    return script.build()


def _budget_for(scenario: BenchmarkScenario) -> Budget:
    if scenario.budget_override is None:
        return UNLIMITED_BUDGET
    return Budget.model_validate(scenario.budget_override)


async def _seed_rows(database: Database, execution_id: str, workflow: Workflow) -> None:
    async with database.session() as session:
        await WorkflowRepository(session).save(workflow)
        await ExecutionRepository(session).create(
            execution_id=execution_id,
            workflow_id=workflow.id,
            task_description="benchmark scenario",
        )


def _first_action_from_events(sink: InMemoryEventSink) -> SupervisorAction | None:
    for event in sink.events:
        if event.type.value == "supervisor_decided":
            action = event.payload.get("action")
            if isinstance(action, str):
                return SupervisorAction(action)
    return None


async def run_scenario(
    scenario: BenchmarkScenario,
    arm: Arm,
    *,
    database: Database,
    redis: RedisCoordinator,
    sandbox_root: Path | None = None,
) -> ScenarioResult:
    """Execute one scenario under one arm, returning a graded result.

    ``redis`` is accepted for interface symmetry with the rest of the engine's
    harnesses (and for a future arm that exercises cross-process coordination)
    but is not otherwise used: nothing in a single scenario run needs
    distributed locking or the event stream.
    """
    del redis
    execution_id = new_id("execution")
    # A minimal, always-constructible state: if setup itself fails (a bad
    # budget_override, a malformed mock_script), judge() still needs a real
    # ExecutionState and Workflow to grade against, not just an error string.
    state = ExecutionState(
        execution_id=execution_id,
        workflow_id="wkf_pending",
        task=Task(description=scenario.task, inputs=scenario.inputs),
    )
    tool_results: list[ToolResult] = []

    start = time.perf_counter()
    error: str | None = None
    workflow: Workflow = Workflow(
        name="error", nodes=(WorkflowNode(id="x", kind=NodeKind.TERMINAL),)
    )
    first_action: SupervisorAction | None = None

    try:
        agents = build_default_agent_registry()
        tools = build_default_registry()
        policy = build_default_policy_engine(agents=agents, tools=tools)
        provider = _build_provider(scenario)
        llm = LLMClient.mock(provider, sleep=_no_sleep)
        router = build_default_router(mock_only=True, configured_providers=["mock"])

        budget = _budget_for(scenario)
        state.budget = budget
        state.budget_usage = BudgetUsage()
        meter = BudgetMeter(budget, state.budget_usage)

        async def observe_tool(_agent_id: str, result: ToolResult) -> None:
            tool_results.append(result)

        if arm.uses_supervisor:
            workflow, first_action = await _run_supervised(
                scenario,
                arm,
                state,
                agents,
                tools,
                policy,
                llm,
                router,
                meter,
                database,
                observe_tool,
                sandbox_root,
            )
        else:
            workflow, first_action = await _run_baseline(
                state, agents, tools, policy, llm, router, meter, observe_tool, sandbox_root
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    latency_seconds = time.perf_counter() - start
    return judge(
        scenario,
        arm=arm.name,
        workflow=workflow,
        state=state,
        tool_results=tool_results,
        first_action=first_action,
        latency_seconds=latency_seconds,
        error=error,
    )


async def _run_supervised(
    scenario: BenchmarkScenario,
    arm: Arm,
    state: ExecutionState,
    agents: AgentRegistry,
    tools: ToolRegistry,
    policy: PolicyEngine,
    llm: LLMClient,
    router: ModelRouter,
    meter: BudgetMeter,
    database: Database,
    observe_tool: object,
    sandbox_root: Path | None,
) -> tuple[Workflow, SupervisorAction | None]:
    workflow = seed_dynamic_workflow()
    state.workflow_id = workflow.id
    await _seed_rows(database, state.execution_id, workflow)

    approvals = ApprovalService(database)
    sink = InMemoryEventSink()
    events = ExecutionEventRecorder(bus=EventBus([sink]), execution_id=state.execution_id)

    async def authorise(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
        decision = policy.evaluate(agent_id, tool, arguments)
        if decision.allowed:
            policy.record_call(agent_id, tool)
        return decision.effect, decision.reason

    runtime = AgentRuntime(
        llm=llm,
        tools=tools,
        router=router,
        authoriser=approvals.tool_authoriser(  # type: ignore[arg-type]
            authorise, execution_id=state.execution_id
        ),
        budget_check=BudgetGuard(meter),
        tool_observer=observe_tool,  # type: ignore[arg-type]
    )
    supervisor = Supervisor(agents=agents, llm=llm, router=router, tools=tools)
    orchestrator = ExecutionOrchestrator(
        supervisor=supervisor,
        agents=agents,
        tools=tools,
        runtime=runtime,
        approvals=approvals,
        meter=meter,
        events=events,
        sandbox_root=sandbox_root,
        max_concurrent_nodes=arm.max_concurrent_nodes,
        node_retry_policy=arm.retry_policy,
    )

    result = await orchestrator.run(state, workflow)
    # A scenario scripted to pause for approval decides automatically here,
    # then resumes -- so the benchmark stays unattended end to end. Bounded to
    # a handful of rounds: a scenario that pauses more than that is a scripting
    # bug, not a legitimate multi-approval flow this benchmark exercises.
    for _ in range(5):
        if state.status is not ExecutionStatus.WAITING_FOR_APPROVAL:
            break
        pending = await approvals.pending_for(state.execution_id)
        if not pending:
            break
        for request in pending:
            if scenario.auto_reject:
                await approvals.reject(request.id, by="benchmark")
            else:
                await approvals.approve(request.id, by="benchmark")
        result = await orchestrator.run(state, result.workflow)

    return result.workflow, _first_action_from_events(sink)


async def _run_baseline(
    state: ExecutionState,
    agents: AgentRegistry,
    tools: ToolRegistry,
    policy: PolicyEngine,
    llm: LLMClient,
    router: ModelRouter,
    meter: BudgetMeter,
    observe_tool: object,
    sandbox_root: Path | None,
) -> tuple[Workflow, SupervisorAction | None]:
    """The no-supervisor arm: one heuristically-chosen agent, run once.

    Uses the engine's own deterministic keyword/capability matching -- the
    same :class:`HeuristicRouter` the LLM-driven path falls back to -- so this
    is a genuine "no LLM supervisor" comparison, not a strawman that ignores
    routing altogether.
    """
    decision = HeuristicRouter(agents).decide(state)
    workflow_id = new_id("workflow")
    state.workflow_id = workflow_id
    state.transition_to(ExecutionStatus.RUNNING)

    if not decision.requires_agents:
        if decision.action in {SupervisorAction.RESPOND_DIRECTLY, SupervisorAction.FINALIZE}:
            state.final_output = decision.answer
            state.transition_to(ExecutionStatus.SUCCEEDED)
        else:
            state.transition_to(
                ExecutionStatus.FAILED, reason=decision.failure_reason or "no agent matched"
            )
        # Workflow.nodes requires at least one node even when the baseline
        # never delegates at all (respond_directly/finalize/fail) -- a plain
        # terminal placeholder, never actually scheduled by anything here.
        placeholder = WorkflowNode(id="done", kind=NodeKind.TERMINAL)
        return Workflow(id=workflow_id, name="baseline", nodes=(placeholder,)), decision.action

    target = decision.targets[0]
    node = WorkflowNode(id=target.agent_id, kind=NodeKind.AGENT, agent_id=target.agent_id)
    workflow = Workflow(id=workflow_id, name="baseline", nodes=(node,))

    async def authorise(agent_id: str, tool: str, arguments: JsonDict) -> tuple[PolicyEffect, str]:
        decision2 = policy.evaluate(agent_id, tool, arguments)
        if decision2.allowed:
            policy.record_call(agent_id, tool)
        return decision2.effect, decision2.reason

    runtime = AgentRuntime(
        llm=llm,
        tools=tools,
        router=router,
        authoriser=authorise,
        budget_check=BudgetGuard(meter),
        tool_observer=observe_tool,  # type: ignore[arg-type]
    )
    node_state = state.node_state(target.agent_id)
    node_state.mark_running()
    context = AgentRunContext(
        execution_id=state.execution_id,
        node_id=target.agent_id,
        instruction=target.instruction,
        sandbox_root=sandbox_root,
        seed=target.agent_id,
    )
    try:
        run_result = await runtime.run(agents.get(target.agent_id), context)
    except BudgetExceededError as exc:
        # A genuine engine-level stop, not a node failure -- WorkflowExecutor
        # re-raises this past its own node-retry handling rather than treating
        # it as a normal failure, and baseline must draw the same distinction.
        node_state.mark_failed({"code": "budget_exceeded", "message": str(exc)})
        state.transition_to(ExecutionStatus.BUDGET_EXCEEDED, reason=str(exc))
        return workflow, decision.action
    except Exception as exc:
        # Baseline has no node-level retry wrapper (that is WorkflowExecutor's
        # job, and there is no executor here) -- an exception the agent's own
        # LLM-level retry could not absorb is therefore an immediate, total
        # failure of this one and only attempt, exactly as if node_retry_policy
        # were max_attempts=1.
        node_state.mark_failed({"code": type(exc).__name__, "message": str(exc)})
        state.transition_to(ExecutionStatus.FAILED, reason=str(exc))
        return workflow, decision.action

    # AgentRuntime tracks token/cost/tool usage only on the local
    # AgentInvocation record it returns -- WorkflowExecutor is what feeds that
    # into the shared BudgetMeter once a node completes (executor.py, right
    # after its own `runtime.run()` call). There is no orchestrator here to do
    # that, so it must happen explicitly, or a budget scenario would never
    # actually observe its own agent's consumption.
    meter.record_agent_step()
    meter.record_llm_usage(
        input_tokens=run_result.invocation.input_tokens,
        output_tokens=run_result.invocation.output_tokens,
        cost_usd=run_result.invocation.cost_usd,
    )
    meter.record_tool_call(run_result.invocation.tool_calls)

    try:
        meter.check(f"agent:{target.agent_id}:final")
    except BudgetExceededError as exc:
        node_state.mark_failed({"code": "budget_exceeded", "message": str(exc)})
        state.transition_to(ExecutionStatus.BUDGET_EXCEEDED, reason=str(exc))
        return workflow, decision.action

    if run_result.succeeded and run_result.output is not None:
        node_state.mark_succeeded(confidence=run_result.output.confidence)
        state.record_agent_output(
            target.agent_id, run_result.output.model_dump(mode="json"), output_key=target.agent_id
        )
        state.final_output = run_result.output.content
        state.transition_to(ExecutionStatus.SUCCEEDED)
    else:
        error = run_result.invocation.error or {"code": "unknown", "message": "agent failed"}
        node_state.mark_failed(error)
        state.transition_to(ExecutionStatus.FAILED, reason=error.get("message", "agent failed"))
    return workflow, decision.action
