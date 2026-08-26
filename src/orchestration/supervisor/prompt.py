"""Supervisor prompt construction.

The supervisor's prompt is the highest-leverage text in the system, so it is
built here deliberately rather than assembled inline at the call site.

Three constraints shaped it:

**It must describe a closed set of actions.**
    The engine dispatches on :class:`SupervisorAction`. An action the prompt does
    not name is an action the model should not invent, so the prompt enumerates
    them with the conditions each requires.

**It must be small.**
    Routing happens on every step, so every token here is paid repeatedly. Agent
    summaries exclude system prompts; prior outputs are truncated; only the
    shortlisted agents are described when a shortlist exists.

**It must state what has already happened.**
    A supervisor that cannot see prior work re-delegates forever. The state
    digest is therefore explicit about completed nodes, failures, retries and
    remaining budget.
"""

from __future__ import annotations

import json

from orchestration.domain.base import JsonDict
from orchestration.domain.budget import BudgetSnapshot
from orchestration.domain.execution import ExecutionState
from orchestration.domain.tool import ToolSpec

SUPERVISOR_SYSTEM_PROMPT = """
You are the supervisor of a multi-agent system. You do not perform work yourself:
you decide what happens next and the engine carries it out.

On each turn you receive the task, the current execution state, and the agents
available to you. You reply with exactly one routing decision as a JSON object.

The available actions and what each requires:

- respond_directly: answer now, without any agent. Requires `answer`.
  Use when the task needs no research, computation, or tool access.

- delegate: hand the work to exactly one agent. Requires one entry in `targets`.
  Use when a single specialist covers the whole remaining task.

- parallel_delegate: run two or more agents concurrently. Requires at least two
  entries in `targets`. Use only when their work is genuinely independent -- if
  one agent needs another's output, delegate sequentially instead.

- retry: re-attempt a node that failed. Requires `retry_node_id`.
  Use when the failure was transient and the node has attempts remaining.

- replan: extend the workflow with new nodes. Requires `plan`.
  Use when what you have learned means the original plan is wrong.

- request_human_approval: pause for human authorisation. Requires
  `approval_action` and `approval_risk_reason`.

- finalize: synthesise everything gathered into the final answer. Requires
  `answer`. Use when the work is done, or when it is as done as the budget or
  the available agents will allow.

- fail: stop and report that the task cannot be completed. Requires
  `failure_reason`. Use when no agent can make progress and there is nothing
  worth reporting.

Rules:

1. Never delegate to an agent that is not in the list you were given.
2. Never delegate the same work to an agent that has already produced it. Read
   the completed outputs before deciding.
3. Prefer parallel_delegate when dimensions are independent -- it is faster and
   costs the same.
4. When an agent reported low confidence or open gaps, address those gaps
   specifically rather than repeating the original instruction.
5. When the budget is nearly exhausted, finalize with what you have. A partial
   answer that says what is missing is more useful than a run that dies.
6. `reason` is mandatory and must state why this action, in one or two sentences.
   It is read by humans debugging the execution afterwards.
7. Set `confidence` to your actual confidence in the decision, not to 1.0 by
   habit.

Reply with only the JSON object. No prose, no code fences.
""".strip()


def build_supervisor_messages(
    *,
    state: ExecutionState,
    agent_summaries: tuple[JsonDict, ...],
    tool_specs: tuple[ToolSpec, ...] = (),
    budget: BudgetSnapshot | None = None,
    extra_instruction: str | None = None,
) -> tuple[str, str]:
    """Build the ``(system, user)`` pair for a supervisor turn.

    Returns a tuple rather than :class:`Message` objects so the caller controls
    message construction -- which matters because some providers need the system
    prompt hoisted out of the conversation entirely.
    """
    sections: list[str] = [
        f"## Task\n{state.task.description}",
    ]

    if state.task.success_criteria:
        sections.append(
            "## Success criteria\n" + "\n".join(f"- {c}" for c in state.task.success_criteria)
        )

    if state.task.inputs:
        sections.append(f"## Inputs\n```json\n{_compact(state.task.inputs)}\n```")

    sections.append("## Available agents\n" + _render_agents(agent_summaries))

    if tool_specs:
        sections.append(
            "## Tools in the system\n"
            + "\n".join(f"- {s.name} (risk: {s.risk.value}): {s.description}" for s in tool_specs)
        )

    sections.append("## Execution state\n" + _render_state(state))

    if state.agent_outputs:
        sections.append("## Completed work\n" + _render_outputs(state))

    if state.errors:
        sections.append("## Failures so far\n" + _render_errors(state))

    if budget is not None:
        sections.append("## Budget\n" + _render_budget(budget))

    if extra_instruction:
        sections.append(f"## Additional instruction\n{extra_instruction}")

    sections.append("Decide the next action and reply with the JSON object.")

    return SUPERVISOR_SYSTEM_PROMPT, "\n\n".join(sections)


def _compact(payload: JsonDict, *, limit: int = 2_000) -> str:
    return json.dumps(payload, default=str, indent=2)[:limit]


def _render_agents(summaries: tuple[JsonDict, ...]) -> str:
    if not summaries:
        return "(none -- you cannot delegate; respond directly, finalize, or fail)"
    lines: list[str] = []
    for summary in summaries:
        capabilities = ", ".join(str(c.get("name", "")) for c in summary.get("capabilities", []))
        tools = ", ".join(str(t) for t in summary.get("tools", [])) or "none"
        lines.append(
            f"- {summary['id']}: {summary['description']}\n"
            f"    capabilities: {capabilities or 'unspecified'}\n"
            f"    tools: {tools}"
        )
    return "\n".join(lines)


def _render_state(state: ExecutionState) -> str:
    summary = state.summary()
    parts = [
        f"- status: {summary['status']}",
        f"- nodes completed: {summary['nodes_succeeded']} succeeded, "
        f"{summary['nodes_failed']} failed",
        f"- agent steps used: {summary['agent_steps']}",
        f"- tool calls used: {summary['tool_calls']}",
        f"- retries so far: {summary['total_retries']}",
        f"- elapsed: {summary['elapsed_seconds']}s",
    ]
    if state.node_states:
        statuses = ", ".join(
            f"{node_id}={node.status.value}" for node_id, node in sorted(state.node_states.items())
        )
        parts.append(f"- node statuses: {statuses}")
    if state.replan_count:
        parts.append(f"- replans performed: {state.replan_count}")
    return "\n".join(parts)


def _render_outputs(state: ExecutionState, *, per_output: int = 2_500) -> str:
    """Render completed outputs, with confidence and gaps made prominent.

    Confidence and gaps come first because they are what should drive the next
    decision; burying them under prose invites the supervisor to accept weak work.
    """
    chunks: list[str] = []
    for node_id, payload in state.agent_outputs.items():
        confidence = payload.get("confidence", "unknown")
        header = f"### {node_id} (confidence: {confidence})"
        body = str(payload.get("content", ""))[:per_output]
        chunk = f"{header}\n{body}"
        gaps = payload.get("gaps")
        if isinstance(gaps, list) and gaps:
            chunk += "\nOPEN GAPS: " + "; ".join(str(g) for g in gaps[:8])
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            chunk += f"\nevidence items: {len(evidence)}"
            if not evidence:
                chunk += " (NONE -- claims here are unsupported)"
        chunks.append(chunk)
    return "\n\n".join(chunks)


def _render_errors(state: ExecutionState, *, limit: int = 8) -> str:
    lines: list[str] = []
    for error in state.errors[-limit:]:
        status = "recovered" if error.recovered else "unresolved"
        lines.append(
            f"- {error.node_id or '(execution)'}: {error.code} "
            f"({'retryable' if error.retryable else 'terminal'}, {status}) "
            f"attempt {error.attempt} -- {error.message[:200]}"
        )
    return "\n".join(lines)


def _render_budget(snapshot: BudgetSnapshot) -> str:
    lines: list[str] = []
    for status in snapshot.statuses:
        if status.limit is None:
            continue
        fraction = status.fraction_used
        marker = ""
        if status.exceeded:
            marker = "  <-- EXHAUSTED"
        elif status.warning:
            marker = "  <-- nearly exhausted, prefer finalize"
        lines.append(
            f"- {status.dimension.value}: {status.used:.4g} / {status.limit:.4g}"
            + (f" ({fraction:.0%})" if fraction is not None else "")
            + marker
        )
    return "\n".join(lines) or "- unmetered"


def build_replan_instruction(reason: str) -> str:
    """Extra instruction used when the supervisor is explicitly asked to replan."""
    return (
        f"The previous plan is no longer adequate: {reason}\n"
        "Return action 'replan' with a `plan` containing the new nodes and the "
        "edges connecting them. Every node must name an agent from the list "
        "above. Attach the new subgraph to existing nodes using `attach_after`."
    )


def build_low_confidence_instruction(node_id: str, confidence: float) -> str:
    """Extra instruction used when an agent returned low-confidence work."""
    return (
        f"Node {node_id!r} returned confidence {confidence:.2f}, which is below the "
        "acceptable threshold. Either delegate follow-up work that targets its "
        "specific gaps, or finalize while stating the uncertainty plainly. Do not "
        "simply re-run the same agent with the same instruction."
    )
