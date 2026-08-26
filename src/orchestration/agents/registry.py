"""Agent registry.

Stores :class:`AgentDefinition` records and answers the supervisor's questions:
which agents exist, what can they do, and which of them are plausible candidates
for a given task.

The candidate shortlisting here is *deterministic* -- keyword and capability
matching, no LLM call. That matters for two reasons: it gives the supervisor a
pre-filtered list so its prompt stays small, and it provides the heuristic
fallback router with a usable answer when the LLM cannot produce a valid decision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator

from orchestration.domain.agent import AgentDefinition
from orchestration.domain.base import JsonDict, utc_now
from orchestration.errors import DuplicateError, NotFoundError
from orchestration.tools.registry import ToolRegistry


class AgentRegistry:
    """A mutable collection of agent definitions."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        self._lock = asyncio.Lock()

    # -- mutation ----------------------------------------------------------

    def register(self, definition: AgentDefinition, *, replace: bool = False) -> AgentDefinition:
        """Add an agent definition.

        Raises:
            DuplicateError: If the id is taken and ``replace`` is false.
        """
        if definition.id in self._agents and not replace:
            raise DuplicateError(
                f"agent {definition.id!r} is already registered",
                agent=definition.id,
                hint="pass replace=True to overwrite",
            )
        self._agents[definition.id] = definition
        return definition

    async def register_async(
        self, definition: AgentDefinition, *, replace: bool = False
    ) -> AgentDefinition:
        async with self._lock:
            return self.register(definition, replace=replace)

    def register_all(
        self, definitions: Iterable[AgentDefinition], *, replace: bool = False
    ) -> None:
        for definition in definitions:
            self.register(definition, replace=replace)

    def update(self, agent_id: str, **changes: object) -> AgentDefinition:
        """Apply a partial update, re-validating the result.

        Uses ``merged`` so an invalid update raises instead of producing a
        malformed definition that would fail later at invocation time.
        """
        existing = self.get(agent_id)
        updated: AgentDefinition = existing.merged(**changes, updated_at=utc_now())
        self._agents[agent_id] = updated
        return updated

    def remove(self, agent_id: str) -> None:
        if agent_id not in self._agents:
            raise NotFoundError(f"agent {agent_id!r} is not registered", agent=agent_id)
        del self._agents[agent_id]

    # -- lookup ------------------------------------------------------------

    def get(self, agent_id: str) -> AgentDefinition:
        """Fetch a definition.

        Raises:
            NotFoundError: If the agent is unknown. The error carries the known
                ids, which turns a typo in a workflow file into an immediately
                actionable message.
        """
        definition = self._agents.get(agent_id)
        if definition is None:
            raise NotFoundError(
                f"agent {agent_id!r} is not registered",
                agent=agent_id,
                available=sorted(self._agents),
            )
        return definition

    def try_get(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def list_agents(self, *, include_disabled: bool = False) -> tuple[AgentDefinition, ...]:
        return tuple(a for _, a in sorted(self._agents.items()) if include_disabled or a.enabled)

    def ids(self, *, include_disabled: bool = False) -> tuple[str, ...]:
        return tuple(a.id for a in self.list_agents(include_disabled=include_disabled))

    def by_kind(self, kind: str) -> tuple[AgentDefinition, ...]:
        return tuple(a for a in self.list_agents() if a.kind == kind)

    def by_capability(self, capability: str) -> tuple[AgentDefinition, ...]:
        return tuple(
            a for a in self.list_agents() if any(c.name == capability for c in a.capabilities)
        )

    def with_tool(self, tool: str) -> tuple[AgentDefinition, ...]:
        """Agents permitted to use ``tool``."""
        return tuple(a for a in self.list_agents() if a.may_attempt(tool))

    # -- supervisor support -------------------------------------------------

    def candidates_for(
        self, task_text: str, *, limit: int = 5, min_score: float = 0.01
    ) -> tuple[tuple[AgentDefinition, float], ...]:
        """Rank enabled agents against ``task_text`` by capability keywords.

        Deterministic and LLM-free, so it can serve both as a prompt pre-filter
        and as the fallback router's answer. Returns ``(definition, score)`` pairs
        sorted by descending score; ties break on agent id so the ordering is
        stable across runs, which the benchmark relies on.
        """
        scored = [(agent, agent.capability_score(task_text)) for agent in self.list_agents()]
        ranked = [(a, s) for a, s in scored if s >= min_score]
        ranked.sort(key=lambda pair: (-pair[1], pair[0].id))
        return tuple(ranked[:limit])

    def best_for(self, task_text: str) -> AgentDefinition | None:
        """The single highest-scoring agent, or ``None`` if nothing matches."""
        candidates = self.candidates_for(task_text, limit=1)
        return candidates[0][0] if candidates else None

    def summaries_for_supervisor(
        self, *, only: Iterable[str] | None = None
    ) -> tuple[JsonDict, ...]:
        """Compact agent descriptions for the supervisor prompt.

        Args:
            only: Restrict to these ids -- used with :meth:`candidates_for` to
                keep the routing prompt small on a large registry.
        """
        agents = self.list_agents()
        if only is not None:
            wanted = set(only)
            agents = tuple(a for a in agents if a.id in wanted)
        return tuple(a.summary_for_supervisor() for a in agents)

    # -- validation --------------------------------------------------------

    def validate_against_tools(self, tools: ToolRegistry) -> tuple[str, ...]:
        """Report agents whose allowlists reference unknown tools.

        Returns problems rather than raising: a deployment may intentionally omit
        an optional tool, and the caller decides whether that is fatal. The API
        surfaces this as a warning; the workflow validator treats a *referenced*
        missing tool as an error.
        """
        problems: list[str] = []
        for agent in self.list_agents(include_disabled=True):
            for permission in agent.allowed_tools:
                if not tools.has(permission.tool):
                    problems.append(
                        f"agent {agent.id!r} allows unregistered tool {permission.tool!r}"
                    )
                elif not tools.is_enabled(permission.tool):
                    problems.append(
                        f"agent {agent.id!r} allows tool {permission.tool!r} "
                        "which is registered but disabled"
                    )
        return tuple(problems)

    # -- dunder ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._agents

    def __iter__(self) -> Iterator[AgentDefinition]:
        return iter(self.list_agents())

    def __repr__(self) -> str:
        return f"<AgentRegistry agents={len(self._agents)}>"
