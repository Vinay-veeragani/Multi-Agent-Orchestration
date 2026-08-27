"""The benchmark scenario library.

Every scenario is fully deterministic: the exact LLM replies are scripted
(:data:`BenchmarkScenario.mock_script`) and any failures are scripted too
(:data:`BenchmarkScenario.fault_injection`), so the same scenario produces the
same trajectory on every run under a given arm -- which is what makes a
change in a benchmark number a real regression rather than model variance.

Categories, and what each is actually measuring:

``simple`` (8)
    One agent, one round. The floor every arm should clear.
``parallel`` (6)
    Several agents fanned out in one decision. Only ``supervisor-parallel``
    can realise the wall-clock benefit; every arm can still *complete* it.
``chain`` (6)
    Two sequential delegations. Tests that the supervisor uses the first
    agent's output to inform the second decision.
``retry`` (6)
    The delegated agent's first attempt faults; a retry succeeds. Only the
    retry-enabled arms (``supervisor-retry``, ``supervisor-parallel``) should
    pass -- ``baseline`` and ``supervisor`` are *expected* to fail these, and
    that gap is the point.
``tool`` (6)
    The agent must call a specific tool and succeed.
``deny`` (5)
    The agent asks for a tool outside its allowlist; policy must refuse it
    without the run breaking.
``approval`` (7)
    A supervisor-requested human approval, auto-approved or auto-rejected so
    the benchmark stays unattended.
``budget`` (4)
    A budget too tight to complete the task; the run must stop cleanly with
    ``BUDGET_EXCEEDED`` rather than exhausting it silently.
``fail`` (3)
    A task nothing can serve; the engine must say so rather than hang.
``respond`` (3)
    No delegation needed at all -- the supervisor answers directly.
"""

from __future__ import annotations

import json

from orchestration.domain.enums import ExecutionStatus, SupervisorAction
from orchestration.domain.evaluation import BenchmarkScenario, ScenarioExpectation
from orchestration.llm.mock import agent_output, routing_decision


def _tool_call(tool: str, arguments: dict[str, object]) -> str:
    """An inline tool-call reply, the shape a locally-hosted model tends to emit."""
    return json.dumps({"tool_calls": [{"name": tool, "arguments": arguments}]})


# ---------------------------------------------------------------------------
# simple: one agent, one round
# ---------------------------------------------------------------------------

_SIMPLE_TASKS: tuple[tuple[str, str, str], ...] = (
    ("research_agent", "find independent sources comparing CRM vendors", "five vendors found"),
    (
        "pricing_agent",
        "find the price per seat for the top project management tools",
        "pricing found",
    ),
    (
        "feature_agent",
        "find what AI capabilities the leading note-taking apps ship",
        "features found",
    ),
    ("data_agent", "profile the schema and columns of the uploaded dataset", "schema profiled"),
    ("code_agent", "find the function definitions handling authentication", "functions found"),
    ("analyst_agent", "analyse why signups dropped last quarter", "analysis complete"),
    ("critic_agent", "review this report for unsupported claims", "review complete"),
    ("finalizer_agent", "write the final report from the completed research", "report written"),
)


def _simple_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for agent_id, task, answer in _SIMPLE_TASKS:
        short = agent_id.removesuffix("_agent")
        scenarios.append(
            BenchmarkScenario(
                id=f"simple-{short}",
                category="simple",
                description=f"A single-agent task matching {agent_id}'s specialty.",
                task=task,
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    first_action=SupervisorAction.DELEGATE,
                    required_agents=frozenset({agent_id}),
                    output_contains=(answer,),
                ),
                mock_script={
                    "supervisor": [
                        routing_decision("delegate", agents=[agent_id]),
                        routing_decision("finalize", answer=answer),
                    ],
                    agent_id: [agent_output(answer)],
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# parallel: several agents fanned out together
# ---------------------------------------------------------------------------

_PARALLEL_COMBOS: tuple[tuple[str, ...], ...] = (
    ("research_agent", "pricing_agent"),
    ("research_agent", "pricing_agent", "feature_agent"),
    ("data_agent", "code_agent"),
    ("research_agent", "critic_agent"),
    ("pricing_agent", "feature_agent"),
    ("data_agent", "code_agent", "analyst_agent"),
)


def _parallel_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, agents in enumerate(_PARALLEL_COMBOS, start=1):
        answer = f"combined findings from {', '.join(agents)}"
        mock_script = {
            "supervisor": [
                routing_decision("parallel_delegate", agents=list(agents)),
                routing_decision("finalize", answer=answer),
            ],
        }
        for agent_id in agents:
            mock_script[agent_id] = [agent_output(f"partial finding from {agent_id}")]
        scenarios.append(
            BenchmarkScenario(
                id=f"parallel-{index}",
                category="parallel",
                description=f"Fan out to {len(agents)} independent agents: {', '.join(agents)}.",
                task=f"gather {', '.join(a.removesuffix('_agent') for a in agents)} "
                "information on competing products, all independent of each other",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    first_action=SupervisorAction.PARALLEL_DELEGATE,
                    required_agents=frozenset(agents),
                    expects_parallelism=True,
                ),
                mock_script=mock_script,
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# chain: sequential delegation, second round informed by the first
# ---------------------------------------------------------------------------

_CHAINS: tuple[tuple[str, str], ...] = (
    ("research_agent", "analyst_agent"),
    ("data_agent", "analyst_agent"),
    ("research_agent", "critic_agent"),
    ("code_agent", "analyst_agent"),
    ("pricing_agent", "finalizer_agent"),
    ("analyst_agent", "finalizer_agent"),
)


def _chain_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, (first, second) in enumerate(_CHAINS, start=1):
        answer = f"synthesis of {first} and {second}"
        scenarios.append(
            BenchmarkScenario(
                id=f"chain-{index}",
                category="chain",
                description=f"Two-round delegation: {first} then {second}.",
                task=f"research the topic then have it {second.removesuffix('_agent')}d",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    first_action=SupervisorAction.DELEGATE,
                    required_agents=frozenset({first, second}),
                ),
                mock_script={
                    "supervisor": [
                        routing_decision("delegate", agents=[first]),
                        routing_decision("delegate", agents=[second]),
                        routing_decision("finalize", answer=answer),
                    ],
                    first: [agent_output(f"{first} output")],
                    second: [agent_output(answer)],
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# retry: a transient fault on the first attempt, recovered on the second
# ---------------------------------------------------------------------------

#: Deliberately avoids research_agent/pricing_agent/feature_agent: they share
#: RESEARCH_AGENT's NETWORK_RETRY_POLICY (max_attempts=5), which the fault
#: window below is sized against the *default* agent retry policy
#: (max_attempts=3) -- a more lenient agent-level policy would absorb the
#: fault entirely on its own, recovering before node-level retry ever gets
#: involved, which would test the wrong layer.
_RETRY_CASES: tuple[tuple[str, str], ...] = (
    ("data_agent", "timeout"),
    ("code_agent", "provider_unavailable"),
    ("data_agent", "rate_limit"),
    ("code_agent", "network"),
    ("analyst_agent", "timeout"),
    ("critic_agent", "provider_unavailable"),
)


def _retry_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, (agent_id, fault_kind) in enumerate(_RETRY_CASES, start=1):
        answer = "recovered after a transient failure"
        scenarios.append(
            BenchmarkScenario(
                id=f"retry-{index}",
                category="retry",
                description=(
                    f"{agent_id} faults with {fault_kind!r} through its own LLM-level "
                    "retry budget, then succeeds only if the node itself is retried."
                ),
                task=f"delegate a single task to {agent_id.removesuffix('_agent')}",
                expectation=ScenarioExpectation(
                    expects_retry=True,
                    expects_recovery=True,
                ),
                mock_script={
                    "supervisor": [
                        routing_decision("delegate", agents=[agent_id]),
                        routing_decision("finalize", answer=answer),
                    ],
                    agent_id: [agent_output(answer)],
                },
                # LLMClient itself retries a transient fault up to its own
                # default policy's max_attempts (3) before ever raising to the
                # node -- so failing only attempt 1 would be invisible to node-
                # level retry, recovering silently inside the LLM client. Attempts
                # 1-3 exhaust that budget, so the 4th call (only reachable via a
                # genuine node-level retry) is what recovery actually depends on.
                fault_injection={agent_id: {"error": fault_kind, "attempts": [1, 2, 3]}},
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# tool: a required tool call that must succeed
# ---------------------------------------------------------------------------

_TOOL_CASES: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("analyst_agent", "calculator", {"expression": "120 * 12"}),
    ("analyst_agent", "calculator", {"expression": "(45 + 55) / 2"}),
    ("research_agent", "web_search", {"query": "CRM pricing comparison"}),
    ("research_agent", "web_search", {"query": "project management tools 2026"}),
    ("critic_agent", "web_search", {"query": "verify vendor claim"}),
    ("pricing_agent", "web_search", {"query": "subscription tier pricing"}),
)


def _tool_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, (agent_id, tool, arguments) in enumerate(_TOOL_CASES, start=1):
        answer = f"used {tool} successfully"
        scenarios.append(
            BenchmarkScenario(
                id=f"tool-{index}",
                category="tool",
                description=f"{agent_id} must call {tool} and succeed.",
                task=f"use {tool.replace('_', ' ')} to help answer this",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    required_agents=frozenset({agent_id}),
                    required_tools=frozenset({tool}),
                ),
                mock_script={
                    "supervisor": [
                        routing_decision("delegate", agents=[agent_id]),
                        routing_decision("finalize", answer=answer),
                    ],
                    agent_id: [_tool_call(tool, arguments), agent_output(answer)],
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# deny: a tool outside the agent's allowlist must be refused, not run
# ---------------------------------------------------------------------------

_DENY_CASES: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("research_agent", "python_exec", {"code": "print(1)"}),
    ("research_agent", "write_file", {"path": "x.txt", "content": "x"}),
    ("critic_agent", "python_exec", {"code": "print(1)"}),
    ("pricing_agent", "python_exec", {"code": "print(1)"}),
    ("analyst_agent", "web_search", {"query": "not allowed for analyst"}),
)


def _deny_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, (agent_id, tool, arguments) in enumerate(_DENY_CASES, start=1):
        answer = "finished without the disallowed tool"
        scenarios.append(
            BenchmarkScenario(
                id=f"deny-{index}",
                category="deny",
                description=f"{agent_id} asks for {tool}, outside its allowlist.",
                task="do the work using whatever tool seems most convenient",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    required_agents=frozenset({agent_id}),
                    forbidden_tools=frozenset({tool}),
                ),
                mock_script={
                    "supervisor": [
                        routing_decision("delegate", agents=[agent_id]),
                        routing_decision("finalize", answer=answer),
                    ],
                    agent_id: [_tool_call(tool, arguments), agent_output(answer)],
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# approval: a supervisor-requested human decision
# ---------------------------------------------------------------------------

_APPROVE_ACTIONS: tuple[str, ...] = (
    "publish the report externally",
    "send the summary email to the customer",
    "delete the temporary workspace",
    "post the finding to the shared channel",
)
_REJECT_ACTIONS: tuple[str, ...] = (
    "delete the production dataset",
    "send an email to the entire customer list",
    "publish unverified pricing claims",
)


def _approval_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, action in enumerate(_APPROVE_ACTIONS, start=1):
        scenarios.append(
            BenchmarkScenario(
                id=f"approval-approve-{index}",
                category="approval",
                description=f"A request to {action!r}, approved.",
                task=f"prepare to {action}",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    first_action=SupervisorAction.REQUEST_HUMAN_APPROVAL,
                    expects_approval=True,
                ),
                mock_script={
                    "supervisor": [
                        routing_decision(
                            "request_human_approval",
                            approval_action=action,
                            approval_risk_reason=f"{action} is externally visible",
                        ),
                        routing_decision("finalize", answer="approved and completed"),
                    ],
                },
                auto_approve=True,
            )
        )
    for index, action in enumerate(_REJECT_ACTIONS, start=1):
        scenarios.append(
            BenchmarkScenario(
                id=f"approval-reject-{index}",
                category="approval",
                description=f"A request to {action!r}, rejected.",
                task=f"prepare to {action}",
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.FAILED,
                    first_action=SupervisorAction.REQUEST_HUMAN_APPROVAL,
                    expects_approval=True,
                ),
                mock_script={
                    "supervisor": [
                        routing_decision(
                            "request_human_approval",
                            approval_action=action,
                            approval_risk_reason=f"{action} is destructive or high-risk",
                        ),
                        routing_decision(
                            "fail", failure_reason="the requested action was rejected"
                        ),
                    ],
                },
                auto_reject=True,
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# budget: a ceiling too tight to complete the task
# ---------------------------------------------------------------------------

_BUDGET_AGENTS: tuple[str, ...] = (
    "research_agent",
    "data_agent",
    "code_agent",
    "analyst_agent",
)


def _budget_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, agent_id in enumerate(_BUDGET_AGENTS, start=1):
        scenarios.append(
            BenchmarkScenario(
                id=f"budget-{index}",
                category="budget",
                description=f"A token budget too tight for {agent_id} to finish.",
                task=f"delegate to {agent_id.removesuffix('_agent')} under a very tight budget",
                expectation=ScenarioExpectation(expects_budget_exceeded=True),
                mock_script={
                    "supervisor": [routing_decision("delegate", agents=[agent_id])],
                    agent_id: [agent_output("this should never be reached")],
                },
                budget_override={
                    "max_tokens": 1,
                    "max_cost_usd": None,
                    "max_duration_seconds": None,
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# fail: nothing can serve the request
# ---------------------------------------------------------------------------

_FAIL_TASKS: tuple[str, ...] = (
    "qwertyuiop zxcvbnm asdfghjkl nonsense request",
    "compute the square root of a rumor",
    "translate this task into a language that does not exist",
)


def _fail_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, task in enumerate(_FAIL_TASKS, start=1):
        scenarios.append(
            BenchmarkScenario(
                id=f"fail-{index}",
                category="fail",
                description="A task no agent can plausibly serve.",
                task=task,
                expectation=ScenarioExpectation(status=ExecutionStatus.FAILED),
                mock_script={
                    "supervisor": [
                        routing_decision("fail", failure_reason="no agent can serve this request")
                    ],
                },
            )
        )
    return scenarios


# ---------------------------------------------------------------------------
# respond: no delegation needed
# ---------------------------------------------------------------------------

_RESPOND_CASES: tuple[tuple[str, str], ...] = (
    ("what is 2 + 2", "4"),
    ("what does CRM stand for", "customer relationship management"),
    ("say hello", "hello"),
)


def _respond_scenarios() -> list[BenchmarkScenario]:
    scenarios = []
    for index, (task, answer) in enumerate(_RESPOND_CASES, start=1):
        scenarios.append(
            BenchmarkScenario(
                id=f"respond-{index}",
                category="respond",
                description="A trivial task the supervisor answers without delegating.",
                task=task,
                expectation=ScenarioExpectation(
                    status=ExecutionStatus.SUCCEEDED,
                    first_action=SupervisorAction.RESPOND_DIRECTLY,
                    output_contains=(answer,),
                ),
                mock_script={
                    "supervisor": [routing_decision("respond_directly", answer=answer)],
                },
            )
        )
    return scenarios


ALL_SCENARIOS: tuple[BenchmarkScenario, ...] = (
    *_simple_scenarios(),
    *_parallel_scenarios(),
    *_chain_scenarios(),
    *_retry_scenarios(),
    *_tool_scenarios(),
    *_deny_scenarios(),
    *_approval_scenarios(),
    *_budget_scenarios(),
    *_fail_scenarios(),
    *_respond_scenarios(),
)
