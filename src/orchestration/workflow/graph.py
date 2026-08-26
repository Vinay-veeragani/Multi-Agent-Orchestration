"""Workflow graph analysis and validation.

Wraps a :class:`Workflow` with the whole-graph questions the scheduler needs
answered: is this executable, what can run now, what does a join wait for.

Validation is a *collecting* operation. It returns every problem it finds rather
than raising on the first, because a workflow author -- or a replanning supervisor
handed its own errors -- fixes a list far more efficiently than a sequence of
single failures.

Detected defects:

* cycles (a DAG requirement; the scheduler would otherwise deadlock)
* edges referencing unknown nodes
* unreachable nodes (dead configuration, silently never run)
* unknown agents or tools
* join nodes with too few inbound edges to ever satisfy their policy
* terminal nodes with outbound edges
* self-loops
* duplicate edges
* nodes whose failure handler does not exist
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property

from orchestration.domain.enums import JoinPolicy, NodeKind, NodeStatus
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Workflow, WorkflowEdge, WorkflowNode


class WorkflowGraph:
    """Analysis and scheduling helper over a :class:`Workflow`.

    Adjacency is computed once and cached: the scheduler queries successors and
    predecessors on every step, and recomputing them from the edge tuple each
    time is quadratic in graph size for no reason.
    """

    def __init__(self, workflow: Workflow) -> None:
        self._workflow = workflow

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    # -- adjacency ---------------------------------------------------------

    @cached_property
    def nodes(self) -> dict[str, WorkflowNode]:
        return {n.id: n for n in self._workflow.nodes}

    @cached_property
    def outgoing(self) -> dict[str, tuple[WorkflowEdge, ...]]:
        mapping: dict[str, list[WorkflowEdge]] = defaultdict(list)
        for edge in self._workflow.edges:
            mapping[edge.source].append(edge)
        return {k: tuple(v) for k, v in mapping.items()}

    @cached_property
    def incoming(self) -> dict[str, tuple[WorkflowEdge, ...]]:
        mapping: dict[str, list[WorkflowEdge]] = defaultdict(list)
        for edge in self._workflow.edges:
            mapping[edge.target].append(edge)
        return {k: tuple(v) for k, v in mapping.items()}

    def successors(self, node_id: str) -> tuple[WorkflowEdge, ...]:
        return self.outgoing.get(node_id, ())

    def predecessors(self, node_id: str) -> tuple[WorkflowEdge, ...]:
        return self.incoming.get(node_id, ())

    @cached_property
    def entry_nodes(self) -> tuple[str, ...]:
        return self._workflow.resolved_entry_nodes

    @cached_property
    def terminal_nodes(self) -> tuple[str, ...]:
        """Nodes with no outgoing edges -- where execution can finish."""
        return tuple(n.id for n in self._workflow.nodes if not self.outgoing.get(n.id))

    # -- validation --------------------------------------------------------

    def validate(
        self,
        *,
        known_agents: Iterable[str] | None = None,
        known_tools: Iterable[str] | None = None,
    ) -> list[str]:
        """Return every structural problem found, empty when the graph is valid.

        Args:
            known_agents: When given, agent references are checked against it.
                Omitted during pure structural checks so the graph can be
                validated without a registry.
            known_tools: As above, for tools.
        """
        problems: list[str] = []
        problems.extend(self._check_references())
        problems.extend(self._check_edges())
        problems.extend(self._check_cycles())
        problems.extend(self._check_reachability())
        problems.extend(self._check_joins())
        problems.extend(self._check_terminals())
        problems.extend(self._check_failure_handlers())
        if known_agents is not None:
            problems.extend(self._check_agents(known_agents))
        if known_tools is not None:
            problems.extend(self._check_tools(known_tools))
        return problems

    def _check_references(self) -> list[str]:
        """Edges pointing at nodes that do not exist.

        The Pydantic model already rejects this at construction; re-checking here
        covers graphs assembled by other means (a JSONB round-trip, a merge) and
        keeps the validator self-contained.
        """
        known = set(self.nodes)
        problems: list[str] = []
        for edge in self._workflow.edges:
            if edge.source not in known:
                problems.append(f"edge {edge.source!r} -> {edge.target!r}: unknown source node")
            if edge.target not in known:
                problems.append(f"edge {edge.source!r} -> {edge.target!r}: unknown target node")
        return problems

    def _check_edges(self) -> list[str]:
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for edge in self._workflow.edges:
            if edge.source == edge.target:
                problems.append(f"node {edge.source!r} has a self-loop")
            key = (edge.source, edge.target)
            if key in seen and not edge.is_conditional:
                # Duplicate unconditional edges are harmless but always a
                # mistake, and they distort join arity checks.
                problems.append(f"duplicate unconditional edge {edge.source!r} -> {edge.target!r}")
            seen.add(key)
        return problems

    def _check_cycles(self) -> list[str]:
        """Detect cycles via Kahn's algorithm.

        Kahn rather than DFS colouring because the same computation yields the
        topological order used elsewhere, and the leftover set names exactly the
        nodes involved in cycles -- which is what an author needs to see.
        """
        known = set(self.nodes)
        in_degree = dict.fromkeys(known, 0)
        for edge in self._workflow.edges:
            if edge.target in in_degree and edge.source in known:
                in_degree[edge.target] += 1

        queue = sorted(n for n, d in in_degree.items() if d == 0)
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for edge in self.successors(current):
                if edge.target not in in_degree:
                    continue
                in_degree[edge.target] -= 1
                if in_degree[edge.target] == 0:
                    queue.append(edge.target)
                    queue.sort()

        if visited == len(known):
            return []

        stuck = sorted(n for n, d in in_degree.items() if d > 0)
        return [f"graph contains a cycle involving: {', '.join(stuck)}"]

    def _check_reachability(self) -> list[str]:
        """Nodes no entry point can reach.

        An unreachable node is dead configuration: it will never run, and its
        presence usually means an edge was mistyped. Reporting it is the
        difference between a silently truncated workflow and a fixable error.
        """
        reachable: set[str] = set()
        frontier = list(self.entry_nodes)
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(edge.target for edge in self.successors(current))

        unreachable = sorted(set(self.nodes) - reachable)
        if not unreachable:
            return []
        return [f"unreachable node(s): {', '.join(unreachable)}"]

    def _check_joins(self) -> list[str]:
        problems: list[str] = []
        for node in self._workflow.nodes:
            if node.kind is not NodeKind.JOIN:
                continue
            inbound = len(self.predecessors(node.id))
            if inbound == 0:
                problems.append(f"join node {node.id!r} has no inbound edges")
            elif inbound == 1:
                problems.append(
                    f"join node {node.id!r} has only one inbound edge; a join with "
                    "nothing to join is a pass-through"
                )
            if (
                node.join_policy is JoinPolicy.QUORUM
                and node.quorum is not None
                and node.quorum > inbound
            ):
                problems.append(
                    f"join node {node.id!r} requires a quorum of {node.quorum} "
                    f"but has only {inbound} inbound edge(s), so it can never fire"
                )
        return problems

    def _check_terminals(self) -> list[str]:
        problems: list[str] = []
        for node in self._workflow.nodes:
            if node.kind is NodeKind.TERMINAL and self.outgoing.get(node.id):
                targets = ", ".join(e.target for e in self.successors(node.id))
                problems.append(f"terminal node {node.id!r} has outgoing edges to: {targets}")
        if not self.terminal_nodes:
            problems.append("graph has no terminal node; execution could never finish")
        return problems

    def _check_failure_handlers(self) -> list[str]:
        known = set(self.nodes)
        return [
            f"node {node.id!r} names failure handler {node.on_failure_node!r} which does not exist"
            for node in self._workflow.nodes
            if node.on_failure_node and node.on_failure_node not in known
        ]

    def _check_agents(self, known_agents: Iterable[str]) -> list[str]:
        available = set(known_agents)
        return [
            f"node {node.id!r} references unknown agent {node.agent_id!r}"
            for node in self._workflow.nodes
            if node.kind is NodeKind.AGENT and node.agent_id not in available
        ]

    def _check_tools(self, known_tools: Iterable[str]) -> list[str]:
        available = set(known_tools)
        return [
            f"node {node.id!r} references unknown tool {node.tool!r}"
            for node in self._workflow.nodes
            if node.kind is NodeKind.TOOL and node.tool not in available
        ]

    # -- ordering ----------------------------------------------------------

    def topological_order(self) -> tuple[str, ...]:
        """Nodes in dependency order.

        Ties break alphabetically so the order is deterministic -- the benchmark
        compares trajectories across runs and would otherwise see phantom
        differences from dict iteration order.

        Returns an empty tuple when the graph contains a cycle; callers should
        have validated first.
        """
        in_degree = dict.fromkeys(self.nodes, 0)
        for edge in self._workflow.edges:
            if edge.target in in_degree:
                in_degree[edge.target] += 1

        queue = sorted(n for n, d in in_degree.items() if d == 0)
        order: list[str] = []
        while queue:
            current = queue.pop(0)
            order.append(current)
            for edge in self.successors(current):
                if edge.target not in in_degree:
                    continue
                in_degree[edge.target] -= 1
                if in_degree[edge.target] == 0:
                    queue.append(edge.target)
                    queue.sort()

        return tuple(order) if len(order) == len(self.nodes) else ()

    def depth_of(self, node_id: str) -> int:
        """Longest path length from an entry node, for diagram layout."""
        order = self.topological_order()
        depths = dict.fromkeys(order, 0)
        for current in order:
            for edge in self.successors(current):
                if edge.target in depths:
                    depths[edge.target] = max(depths[edge.target], depths[current] + 1)
        return depths.get(node_id, 0)

    def parallel_groups(self) -> tuple[tuple[str, ...], ...]:
        """Nodes grouped by depth -- the sets that *could* run concurrently.

        Structural potential, not a schedule: conditions and concurrency limits
        determine what actually runs together. Used by the docs and CLI to show
        where parallelism exists in a workflow.
        """
        order = self.topological_order()
        if not order:
            return ()
        buckets: dict[int, list[str]] = defaultdict(list)
        for node_id in order:
            buckets[self.depth_of(node_id)].append(node_id)
        return tuple(tuple(sorted(buckets[d])) for d in sorted(buckets))

    # -- scheduling support -------------------------------------------------

    def dependencies_satisfied(
        self, node_id: str, state: ExecutionState, *, active_edges: set[str] | None = None
    ) -> bool:
        """Whether ``node_id`` may start now.

        For a join node the answer comes from its :class:`JoinPolicy`. For every
        other node kind, all inbound edges that are *active* must come from
        completed nodes.

        Args:
            active_edges: Edge ids whose conditions evaluated true. When
                supplied, inactive edges are ignored -- which is what allows a
                node behind an untaken conditional branch to be skipped rather
                than blocking forever.
        """
        inbound = self.predecessors(node_id)
        if not inbound:
            return True

        node = self.nodes.get(node_id)
        relevant = [
            edge
            for edge in inbound
            if active_edges is None or edge.id in active_edges or not edge.is_conditional
        ]
        if not relevant:
            return False

        if node is not None and node.kind is NodeKind.JOIN:
            return self._join_satisfied(node, relevant, state)

        return all(self._edge_source_complete(edge, state) for edge in relevant)

    @staticmethod
    def _edge_source_complete(edge: WorkflowEdge, state: ExecutionState) -> bool:
        node_state = state.node_states.get(edge.source)
        return node_state is not None and node_state.is_complete

    def _join_satisfied(
        self, node: WorkflowNode, inbound: list[WorkflowEdge], state: ExecutionState
    ) -> bool:
        """Evaluate a join node's policy against upstream progress."""
        statuses = [state.node_states.get(edge.source) for edge in inbound]
        succeeded = sum(1 for s in statuses if s is not None and s.status is NodeStatus.SUCCEEDED)
        settled = sum(1 for s in statuses if s is not None and s.is_terminal)
        total = len(inbound)

        match node.join_policy:
            case JoinPolicy.ALL:
                return succeeded == total
            case JoinPolicy.ANY:
                return succeeded >= 1
            case JoinPolicy.QUORUM:
                return succeeded >= (node.quorum or total)
            case JoinPolicy.ALL_SETTLED:
                # Tolerates failures: this is how partial results reach a
                # downstream analyst instead of the whole branch being lost.
                return settled == total

    def satisfied_by(self, node_id: str, state: ExecutionState) -> tuple[str, ...]:
        """Which upstream nodes had completed when ``node_id`` became ready."""
        return tuple(
            edge.source
            for edge in self.predecessors(node_id)
            if self._edge_source_complete(edge, state)
        )

    def blocked_reason(self, node_id: str, state: ExecutionState) -> str:
        """Human-readable explanation of why a node is not yet ready."""
        pending = [
            edge.source
            for edge in self.predecessors(node_id)
            if not self._edge_source_complete(edge, state)
        ]
        if not pending:
            return "no unmet dependencies"
        return "waiting on: " + ", ".join(sorted(pending))

    # -- rendering ---------------------------------------------------------

    def to_mermaid(self, state: ExecutionState | None = None) -> str:
        """Render the graph, optionally annotated with live node status.

        Generated from the same definition the engine executes, so a diagram in
        the docs cannot drift from the actual behaviour.
        """
        if state is None:
            return self._workflow.to_mermaid()

        status_class = {
            NodeStatus.SUCCEEDED: "done",
            NodeStatus.FAILED: "failed",
            NodeStatus.RUNNING: "active",
            NodeStatus.SKIPPED: "skipped",
            NodeStatus.WAITING_FOR_APPROVAL: "waiting",
        }
        lines = [self._workflow.to_mermaid()]
        lines.append("    classDef done fill:#d4edda,stroke:#28a745;")
        lines.append("    classDef failed fill:#f8d7da,stroke:#dc3545;")
        lines.append("    classDef active fill:#cce5ff,stroke:#007bff;")
        lines.append("    classDef skipped fill:#e2e3e5,stroke:#6c757d;")
        lines.append("    classDef waiting fill:#fff3cd,stroke:#ffc107;")
        for node_id, node_state in sorted(state.node_states.items()):
            css = status_class.get(node_state.status)
            if css and node_id in self.nodes:
                lines.append(f"    class {node_id} {css};")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"<WorkflowGraph name={self._workflow.name!r} "
            f"nodes={len(self.nodes)} edges={len(self._workflow.edges)}>"
        )


def validate_workflow(
    workflow: Workflow,
    *,
    known_agents: Iterable[str] | None = None,
    known_tools: Iterable[str] | None = None,
) -> list[str]:
    """Convenience wrapper: validate a workflow and return its problems."""
    return WorkflowGraph(workflow).validate(known_agents=known_agents, known_tools=known_tools)


def require_valid_workflow(
    workflow: Workflow,
    *,
    known_agents: Iterable[str] | None = None,
    known_tools: Iterable[str] | None = None,
) -> WorkflowGraph:
    """Validate and return a graph, raising with every problem on failure.

    Raises:
        GraphValidationError: Carrying the complete problem list.
    """
    from orchestration.errors import GraphValidationError

    graph = WorkflowGraph(workflow)
    problems = graph.validate(known_agents=known_agents, known_tools=known_tools)
    if problems:
        raise GraphValidationError(
            f"workflow {workflow.name!r} is not executable",
            problems=problems,
            workflow=workflow.name,
        )
    return graph
