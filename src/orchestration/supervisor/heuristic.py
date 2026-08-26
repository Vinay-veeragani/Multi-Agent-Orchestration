"""Deterministic fallback router.

When the LLM cannot produce a valid :class:`RoutingDecision` -- after the repair
attempt -- the engine does not loop and does not guess. It falls back here.

This matters more than it might appear. Without a fallback, a model outage or a
persistently malformed reply means the execution fails outright. With one, the
system degrades to a simpler but still-correct routing policy and records that it
did so, which is the difference between an outage and a stall.

The policy is intentionally boring and entirely explainable:

* No candidate agents at all -> answer directly, or fail if there is nothing to
  say.
* One clear candidate -> delegate to it.
* Several strong, independent candidates -> delegate in parallel.
* Everything already done -> finalize.
* A failed node with retries remaining -> retry it.

Because it is pure and deterministic, it is also what makes the "supervisor
degraded" path testable rather than hypothetical.
"""

from __future__ import annotations

from orchestration.agents.registry import AgentRegistry
from orchestration.domain.agent import AgentDefinition
from orchestration.domain.enums import NodeStatus, SupervisorAction
from orchestration.domain.execution import ExecutionState
from orchestration.domain.routing import DelegationTarget, RoutingDecision

#: Most agents the fallback will fan out to at once.
MAX_PARALLEL_BRANCHES = 3

#: Confidence reported for fallback decisions. Deliberately middling: the
#: fallback is a reasonable guess, and claiming high confidence for a heuristic
#: would corrupt any downstream confidence-based branching.
FALLBACK_CONFIDENCE = 0.4


class HeuristicRouter:
    """Rule-based router used when structured LLM routing is unavailable."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        max_parallel: int = MAX_PARALLEL_BRANCHES,
        finalizer_agent_id: str = "finalizer_agent",
    ) -> None:
        self._registry = registry
        self._max_parallel = max_parallel
        self._finalizer = finalizer_agent_id

    def decide(self, state: ExecutionState, *, reason_prefix: str = "") -> RoutingDecision:
        """Produce a routing decision from execution state alone.

        Args:
            state: Current execution state.
            reason_prefix: Prepended to the recorded reason, so a trace shows
                *why* the fallback was used and not merely that it was.
        """
        prefix = f"{reason_prefix} " if reason_prefix else ""

        retry = self._retryable_node(state)
        if retry is not None:
            return RoutingDecision(
                action=SupervisorAction.RETRY,
                retry_node_id=retry,
                reason=f"{prefix}heuristic: node {retry!r} failed with a retryable error",
                confidence=FALLBACK_CONFIDENCE,
            )

        already_run = self._agents_already_used(state)
        candidates = [
            (agent, score)
            for agent, score in self._registry.candidates_for(
                self._routing_text(state), limit=self._max_parallel * 2
            )
            if agent.id not in already_run
        ]

        if not candidates:
            return self._terminal_decision(state, prefix)

        parallel = self._complementary_candidates(candidates, self._routing_text(state))

        if len(parallel) >= 2:
            return RoutingDecision(
                action=SupervisorAction.PARALLEL_DELEGATE,
                targets=tuple(
                    DelegationTarget(
                        agent_id=agent.id,
                        instruction=self._instruction_for(agent.id, state),
                        output_key=agent.id,
                    )
                    for agent in parallel
                ),
                reason=(
                    f"{prefix}heuristic: {len(parallel)} agents each cover a distinct "
                    "aspect of the task, so their work is independent"
                ),
                confidence=FALLBACK_CONFIDENCE,
            )

        leader_score = candidates[0][1]
        agent = candidates[0][0]
        return RoutingDecision(
            action=SupervisorAction.DELEGATE,
            targets=(
                DelegationTarget(
                    agent_id=agent.id,
                    instruction=self._instruction_for(agent.id, state),
                    output_key=agent.id,
                ),
            ),
            reason=(
                f"{prefix}heuristic: {agent.id!r} is the strongest single match "
                f"(score {leader_score:.2f})"
            ),
            confidence=FALLBACK_CONFIDENCE,
        )

    # -- helpers -----------------------------------------------------------

    def _complementary_candidates(
        self, candidates: list[tuple[AgentDefinition, float]], text: str
    ) -> list[AgentDefinition]:
        """Select candidates that each cover an aspect the others do not.

        A greedy set cover over the task terms each agent matches. This is what
        makes the fan-out claim honest: two agents run in parallel because they
        address *different* parts of the task, not because their scores happened
        to be close.

        Synthesis agents are excluded -- an analyst running alongside the research
        it is meant to analyse has nothing to work with.
        """
        selected: list[AgentDefinition] = []
        covered: set[str] = set()
        for agent, _score in candidates:
            if agent.is_synthesis_agent:
                continue
            terms = agent.matched_terms(text)
            if not terms:
                continue
            if selected and not (terms - covered):
                # Fully redundant with an already-selected agent: another branch
                # would spend budget re-covering ground.
                continue
            selected.append(agent)
            covered |= terms
            if len(selected) >= self._max_parallel:
                break
        return selected

    def _terminal_decision(self, state: ExecutionState, prefix: str) -> RoutingDecision:
        """Decide how to end when no further delegation is available."""
        if state.agent_outputs:
            summary = self._summarise(state)
            return RoutingDecision(
                action=SupervisorAction.FINALIZE,
                answer=summary,
                reason=(
                    f"{prefix}heuristic: no unused agent matches the remaining work; "
                    f"synthesising {len(state.agent_outputs)} completed output(s)"
                ),
                confidence=FALLBACK_CONFIDENCE,
            )
        return RoutingDecision(
            action=SupervisorAction.FAIL,
            failure_reason=(
                "no agent in the registry matches this task and no work has been "
                "completed, so there is nothing to report"
            ),
            reason=f"{prefix}heuristic: no candidate agents and no prior outputs",
            confidence=FALLBACK_CONFIDENCE,
        )

    @staticmethod
    def _routing_text(state: ExecutionState) -> str:
        """Text the candidate matcher scores against.

        Includes the outstanding gaps from prior outputs, not just the original
        task: after a research pass, what remains to be done is better described
        by the gaps than by the task statement.
        """
        parts = [state.task.description]
        for payload in state.agent_outputs.values():
            gaps = payload.get("gaps") if isinstance(payload, dict) else None
            if isinstance(gaps, list):
                parts.extend(str(g) for g in gaps[:5])
        return " ".join(parts)

    @staticmethod
    def _agents_already_used(state: ExecutionState) -> frozenset[str]:
        """Agents whose output is already recorded.

        Prevents the fallback delegating to the same agent forever, which is the
        obvious failure mode of a stateless heuristic.
        """
        used: set[str] = set()
        for node_id, node_state in state.node_states.items():
            if node_state.status in {NodeStatus.SUCCEEDED, NodeStatus.RUNNING}:
                used.add(node_id)
        used.update(state.agent_outputs)
        # Output keys are frequently the agent id, so treat those as used too.
        used.update(k for k in state.variables if isinstance(k, str))
        return frozenset(used)

    @staticmethod
    def _instruction_for(agent_id: str, state: ExecutionState) -> str:
        """Build a per-agent instruction from the task and outstanding gaps."""
        instruction = state.task.description
        gaps: list[str] = []
        for payload in state.agent_outputs.values():
            if isinstance(payload, dict) and isinstance(payload.get("gaps"), list):
                gaps.extend(str(g) for g in payload["gaps"])
        if gaps:
            instruction += "\n\nOutstanding gaps to address:\n" + "\n".join(
                f"- {g}" for g in gaps[:8]
            )
        return instruction

    @staticmethod
    def _summarise(state: ExecutionState) -> str:
        """Concatenate completed outputs into a plain answer.

        Not an LLM synthesis -- the fallback exists precisely because the LLM is
        unavailable. It is a truthful, if unpolished, presentation of what was
        actually gathered, which is better than fabricating a smooth report.
        """
        lines = ["Summary of completed work (produced without model synthesis):", ""]
        for node_id, payload in state.agent_outputs.items():
            content = str(payload.get("content", "")) if isinstance(payload, dict) else str(payload)
            confidence = payload.get("confidence") if isinstance(payload, dict) else None
            header = f"## {node_id}"
            if confidence is not None:
                header += f" (confidence {confidence})"
            lines.extend([header, content.strip()[:8_000], ""])

            evidence = payload.get("evidence") if isinstance(payload, dict) else None
            if isinstance(evidence, list) and evidence:
                lines.append("Sources: " + "; ".join(str(e) for e in evidence[:10]))
                lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _retryable_node(state: ExecutionState) -> str | None:
        """The first failed node whose last error was retryable."""
        for node_id, node_state in state.node_states.items():
            if node_state.status is not NodeStatus.FAILED:
                continue
            error = node_state.error or {}
            if error.get("retryable") is True:
                return node_id
        return None
