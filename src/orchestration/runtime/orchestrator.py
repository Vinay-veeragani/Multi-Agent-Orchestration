"""Dynamic, supervisor-driven execution.

A static :class:`~orchestration.domain.workflow.Workflow` is executed directly by
:class:`WorkflowExecutor`. When no workflow is supplied, the supervisor decides
the plan as it goes: :class:`ExecutionOrchestrator` is the loop that turns a
sequence of :class:`RoutingDecision` objects into an execution.

The design leans entirely on machinery already built for the static case rather
than duplicating it:

**Delegation compiles to a subgraph, not a special code path.** A
``delegate``/``parallel_delegate`` decision becomes new ``AGENT`` nodes wired
from the current frontier (nodes with no successor yet) to the terminal node,
via :meth:`Workflow.extended_with`. The *same* :class:`WorkflowExecutor` then
runs it -- retries, parallelism, budgets and checkpoints all apply exactly as
they do to a hand-authored workflow, because they are the same object.

**Re-running the executor after every decision is safe because resume already
requires it to be.** :meth:`WorkflowExecutor.run` tolerates being called
against state with existing progress -- that is what checkpoint/resume depends
on -- so calling it again after each supervisor turn to execute only the newly
added nodes needs no special case.

**A supervisor-requested approval uses the same durable-pause mechanism as an
approval workflow node.** ``request_human_approval`` consults
:class:`ApprovalService` directly; an undecided request stops the loop and
returns a paused result, exactly as a static workflow's approval node does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestration.agents.registry import AgentRegistry
from orchestration.agents.runtime import AgentRuntime
from orchestration.budget.meter import BudgetMeter
from orchestration.domain.enums import (
    CheckpointReason,
    EventType,
    ExecutionStatus,
    NodeKind,
    NodeStatus,
    SupervisorAction,
)
from orchestration.domain.execution import ExecutionState
from orchestration.domain.routing import RoutingDecision
from orchestration.domain.workflow import Workflow, WorkflowEdge, WorkflowNode
from orchestration.errors import ApprovalRejectedError, ApprovalRequired, ExecutionCancelledError
from orchestration.events.bus import ExecutionEventRecorder
from orchestration.observability.metrics import record_execution_started
from orchestration.policies.approvals import ApprovalService
from orchestration.supervisor.supervisor import Supervisor
from orchestration.tools.registry import ToolRegistry
from orchestration.workflow.executor import (
    ApprovalGate,
    CancelToken,
    CheckpointWriter,
    ExecutionResult,
    WorkflowExecutor,
    _noop_checkpoint,
)
from orchestration.workflow.graph import WorkflowGraph

#: The synthetic node id every dynamic execution starts with. Kept as a
#: constant, single terminal node so the graph is always structurally valid
#: (§9 requires a terminal node) even before the supervisor has decided anything.
_ROOT_TERMINAL = "final"

#: Hard ceiling on supervisor turns, independent of the budget meter. A
#: misbehaving model that always replans or always finalizes-then-doesn't could
#: otherwise loop until something else stops it; this is the backstop.
MAX_SUPERVISOR_TURNS = 40


def seed_dynamic_workflow(name: str = "dynamic") -> Workflow:
    """The minimal valid graph a dynamic execution starts from.

    A single terminal node. Every delegation the supervisor requests is wired
    in *before* this node, so the graph is always exactly as valid as a
    hand-authored one -- there is no "not yet a real graph" state.

    Each call is a private, execution-scoped graph, not a reusable named
    workflow -- but ``WorkflowRow`` still enforces a ``(name, version)``
    uniqueness constraint meant for the hand-authored case. ``version`` is
    stamped from the generated id's own random suffix so two concurrent
    dynamic executions (which otherwise share ``name="dynamic"`` and the
    default ``version="1.0.0"``) never collide on it.
    """
    workflow = Workflow(
        name=name,
        nodes=(WorkflowNode(id=_ROOT_TERMINAL, kind=NodeKind.TERMINAL),),
        dynamic=True,
    )
    return workflow.model_copy(update={"version": workflow.id.rsplit("_", 1)[-1]})


@dataclass(slots=True)
class OrchestratorResult:
    """Outcome of a dynamic run, mirroring :class:`ExecutionResult`."""

    state: ExecutionState
    workflow: Workflow
    turns: int = 0

    @property
    def status(self) -> ExecutionStatus:
        return self.state.status

    @property
    def succeeded(self) -> bool:
        return self.state.status is ExecutionStatus.SUCCEEDED

    @property
    def is_paused(self) -> bool:
        return self.state.status is ExecutionStatus.WAITING_FOR_APPROVAL


class ExecutionOrchestrator:
    """Drives an execution by repeatedly asking the supervisor what to do next.

    Args:
        supervisor: Produces validated routing decisions.
        agents: Registry the compiled nodes reference.
        tools: Registry for tool-node dispatch (rarely used directly by the
            supervisor, which mostly delegates to agents).
        runtime: Agent runtime shared with the executor.
        approvals: Durable approval store, consulted both for
            ``request_human_approval`` decisions and passed through to the
            executor for any approval nodes a replan introduces.
        meter: Budget enforcement, shared across every executor built during
            the run so consumption accumulates correctly turn to turn.
        events: Event recorder, likewise shared.
        checkpoint: Persists state; a fresh :class:`WorkflowExecutor` is built
            per turn but they all write through the same checkpoint function.
        max_turns: Hard cap on supervisor turns.
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        agents: AgentRegistry,
        tools: ToolRegistry,
        runtime: AgentRuntime,
        approvals: ApprovalService,
        meter: BudgetMeter,
        events: ExecutionEventRecorder,
        checkpoint: CheckpointWriter = _noop_checkpoint,
        cancel_token: CancelToken | None = None,
        sandbox_root: Path | None = None,
        max_concurrent_nodes: int = 8,
        max_turns: int = MAX_SUPERVISOR_TURNS,
    ) -> None:
        self._supervisor = supervisor
        self._agents = agents
        self._tools = tools
        self._runtime = runtime
        self._approvals = approvals
        self._meter = meter
        self._events = events
        self._checkpoint = checkpoint
        self._cancel = cancel_token or CancelToken()
        self._sandbox_root = sandbox_root
        self._max_concurrent_nodes = max_concurrent_nodes
        self._max_turns = max_turns

    async def run(
        self, state: ExecutionState, workflow: Workflow | None = None
    ) -> OrchestratorResult:
        """Run to completion, a pause, or the turn limit.

        Args:
            state: Execution state. On a first call this should be freshly
                constructed; on resume it is whatever :class:`CheckpointManager`
                restored, and the loop continues from exactly that point because
                every decision it needs (completed nodes, prior turns) lives in
                ``state`` and the workflow, not in this object.
            workflow: The graph as it stood at the last checkpoint. Defaults to
                :func:`seed_dynamic_workflow` for a genuinely new execution.
        """
        current = workflow or seed_dynamic_workflow()
        result = OrchestratorResult(state=state, workflow=current)

        # Mirrors WorkflowExecutor.run()'s own entry handling: a fresh execution
        # moves PENDING -> RUNNING and is checkpointed as started; one resumed
        # from a decided approval moves WAITING_FOR_APPROVAL -> RUNNING. Without
        # this, a dynamic run that reaches a terminal action on its very first
        # turn -- before ever calling the executor, which is where the static
        # path performs this transition -- would still be sitting in PENDING.
        if state.status is ExecutionStatus.PENDING:
            record_execution_started()
            state.transition_to(ExecutionStatus.RUNNING)
            await self._events.emit(
                EventType.EXECUTION_STARTED, message="dynamic execution started"
            )
            await self._checkpoint(state, current, CheckpointReason.EXECUTION_STARTED, None)
        elif state.status is ExecutionStatus.WAITING_FOR_APPROVAL:
            state.transition_to(ExecutionStatus.RUNNING)
            await self._events.emit(
                EventType.EXECUTION_RESUMED, message="resumed after approval decision"
            )

        for turn in range(1, self._max_turns + 1):
            result.turns = turn

            try:
                self._cancel.raise_if_cancelled()
            except ExecutionCancelledError as exc:
                await self._handle_cancellation(state, current, exc)
                result.workflow = current
                return result

            outcome = await self._supervisor.decide(state, workflow=current)
            decision = outcome.decision
            await self._events.emit(
                EventType.SUPERVISOR_DECIDED,
                message=decision.reason,
                payload=decision.as_event_payload(),
            )
            if outcome.degraded:
                await self._events.emit(
                    EventType.ROUTING_DEGRADED,
                    message="supervisor decision required the heuristic fallback",
                )

            if decision.action is SupervisorAction.RETRY:
                self._reopen_node(state, decision.retry_node_id)  # type: ignore[arg-type]

            elif decision.requires_agents:
                current = self._compile_delegation(current, decision)

            elif decision.action is SupervisorAction.REPLAN:
                current = self._supervisor.compile_plan(decision.plan, current)  # type: ignore[arg-type]
                state.replan_count += 1
                await self._checkpoint(
                    state, current, CheckpointReason.AFTER_REPLAN, None
                )

            elif decision.action is SupervisorAction.REQUEST_HUMAN_APPROVAL:
                paused = await self._request_approval(state, current, decision)
                if paused:
                    result.workflow = current
                    return result
                continue  # approved: re-decide rather than assuming what comes next

            elif decision.action in {
                SupervisorAction.RESPOND_DIRECTLY,
                SupervisorAction.FINALIZE,
            }:
                state.final_output = decision.answer
                # Whatever was pending is moot once the run concludes -- most
                # commonly because the supervisor decided to wrap up straight
                # after seeing its own prior approval already granted, without
                # re-issuing that decision to have this cleared the ordinary way.
                state.pending_approval_id = None
                state.transition_to(ExecutionStatus.SUCCEEDED)
                await self._checkpoint(
                    state, current, CheckpointReason.EXECUTION_FINALIZED, None
                )
                result.workflow = current
                return result

            elif decision.action is SupervisorAction.FAIL:
                state.pending_approval_id = None
                state.transition_to(
                    ExecutionStatus.FAILED, reason=decision.failure_reason
                )
                await self._checkpoint(
                    state, current, CheckpointReason.EXECUTION_FINALIZED, None
                )
                result.workflow = current
                return result

            # Run the graph as it now stands. Nodes already complete are
            # skipped by the executor's own ready-set computation; only the
            # newly compiled ones (or a re-opened retry) actually execute.
            exec_result = await self._run_executor(state, current)
            if exec_result.is_paused:
                result.workflow = current
                return result
            if state.status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED}:
                # The executor's own verdict, imposed the moment this round's
                # ready-set drained -- not a real conclusion; see
                # _recover_from_round_drain. CANCELLED / BUDGET_EXCEEDED /
                # TIMED_OUT are, by contrast, genuine engine-level stops (the
                # cancel token fired, or the shared meter tripped) and must be
                # allowed to end the run for real.
                await self._recover_from_round_drain(state, current)
            elif state.status.is_terminal:
                result.workflow = current
                return result

        # Turn limit reached without the supervisor concluding. Distinct from a
        # budget exhaustion: the *number of decisions* ran out, not tokens or
        # cost, so it is reported as a plain failure with its own reason.
        state.pending_approval_id = None
        state.transition_to(
            ExecutionStatus.FAILED,
            reason=f"exceeded the maximum of {self._max_turns} supervisor turns",
        )
        await self._checkpoint(state, current, CheckpointReason.EXECUTION_FINALIZED, None)
        result.workflow = current
        return result

    # -- compiling supervisor decisions into graph mutations ----------------

    def _compile_delegation(
        self, workflow: Workflow, decision: RoutingDecision
    ) -> Workflow:
        """Turn a delegate/parallel_delegate decision into new agent nodes.

        Wired from the current frontier -- nodes with no outgoing edge yet,
        other than the terminal -- so each round of delegation happens after
        whatever the previous round produced, and multiple targets in one
        decision run in parallel because they share the same incoming edges.
        """
        frontier = self._frontier(workflow)
        new_nodes = tuple(
            WorkflowNode(
                id=self._unique_node_id(workflow, target.agent_id),
                kind=NodeKind.AGENT,
                agent_id=target.agent_id,
                input_template=target.instruction,
                output_key=target.output_key or target.agent_id,
            )
            for target in decision.targets
        )
        new_edges: list[WorkflowEdge] = []
        for node in new_nodes:
            new_edges.extend(WorkflowEdge(source=src, target=node.id) for src in frontier)
            new_edges.append(WorkflowEdge(source=node.id, target=_ROOT_TERMINAL))
        # The terminal node's old inbound edges from the frontier are
        # superseded by the new agent nodes; nothing needs removing because a
        # terminal node simply takes its content from whichever predecessor
        # completed, and the graph validator does not forbid multiple paths in.
        return workflow.extended_with(new_nodes, tuple(new_edges))

    @staticmethod
    def _frontier(workflow: Workflow) -> tuple[str, ...]:
        """Nodes with no outgoing edge except (optionally) to the terminal.

        These are where the next round of delegation attaches. On the very
        first turn this is just the seed terminal node's sole predecessor slot
        -- i.e. nothing -- so the first delegation attaches directly with no
        predecessors, which is correct: it is the entry point.
        """
        has_real_successor = {
            edge.source for edge in workflow.edges if edge.target != _ROOT_TERMINAL
        }
        leaves = [
            node.id
            for node in workflow.nodes
            if node.id != _ROOT_TERMINAL and node.id not in has_real_successor
        ]
        return tuple(leaves)

    @staticmethod
    def _unique_node_id(workflow: Workflow, agent_id: str) -> str:
        """A node id that cannot collide with one already in the graph.

        An agent may be delegated to more than once across turns (different
        instructions each time), so the id cannot simply be the agent id.
        """
        existing = workflow.node_map
        candidate = agent_id
        suffix = 2
        while candidate in existing:
            candidate = f"{agent_id}-{suffix}"
            suffix += 1
        return candidate

    def _reopen_node(self, state: ExecutionState, node_id: str) -> None:
        """Make a failed node runnable again for a supervisor-ordered retry.

        The supervisor's own semantic validation (checked before this ever
        runs) already confirmed the node exists, failed, and that its failure
        was retryable -- this just clears the terminal marking so the
        executor's ready-set computation picks it up on the next run.
        """
        node_state = state.node_state(node_id)
        node_state.status = NodeStatus.PENDING
        state.record_retry(node_id)
        self._meter.record_retry()

    async def _request_approval(
        self, state: ExecutionState, workflow: Workflow, decision: RoutingDecision
    ) -> bool:
        """Handle a supervisor-requested approval. Returns whether to pause."""
        assert decision.approval_action is not None
        assert decision.approval_risk_reason is not None
        try:
            await self._approvals.require(
                execution_id=state.execution_id,
                action=decision.approval_action,
                risk_reason=decision.approval_risk_reason,
            )
        except ApprovalRequired as pending:
            state.pending_approval_id = pending.approval_id
            await self._checkpoint(
                state, workflow, CheckpointReason.BEFORE_APPROVAL, None
            )
            state.transition_to(ExecutionStatus.WAITING_FOR_APPROVAL)
            await self._checkpoint(
                state, workflow, CheckpointReason.BEFORE_APPROVAL, None
            )
            return True
        except ApprovalRejectedError as exc:
            state.transition_to(ExecutionStatus.FAILED, reason=exc.message)
            await self._checkpoint(
                state, workflow, CheckpointReason.EXECUTION_FINALIZED, None
            )
            return True
        else:
            state.pending_approval_id = None
            return False

    async def _run_executor(
        self, state: ExecutionState, workflow: Workflow
    ) -> ExecutionResult:
        """Build a fresh executor over the current graph and run it.

        Cheap: the executor itself holds no state beyond references to shared
        collaborators, all of which are passed in and persist across turns.
        """
        executor = WorkflowExecutor(
            graph=WorkflowGraph(workflow),
            agents=self._agents,
            tools=self._tools,
            runtime=self._runtime,
            events=self._events,
            meter=self._meter,
            checkpoint=self._checkpoint,
            approval_gate=self._approval_gate(),
            cancel_token=self._cancel,
            max_concurrent_nodes=self._max_concurrent_nodes,
            sandbox_root=self._sandbox_root,
        )
        return await executor.run(state)

    async def _handle_cancellation(
        self, state: ExecutionState, workflow: Workflow, exc: ExecutionCancelledError
    ) -> None:
        """Mirror WorkflowExecutor._handle_cancellation for a between-turns cancel.

        A cancellation that lands mid-node is already handled inside the
        executor (the same :class:`CancelToken` is shared with it); this covers
        the gap where the token fires while the supervisor is deciding or the
        graph is being compiled, with no executor in flight to catch it.
        """
        state.transition_to(ExecutionStatus.CANCELLED, reason=exc.message)
        await self._events.emit(
            EventType.EXECUTION_CANCELLED, message=exc.message, reason=exc.message
        )
        await self._checkpoint(state, workflow, CheckpointReason.ON_CANCELLATION, None)

    async def _recover_from_round_drain(self, state: ExecutionState, workflow: Workflow) -> None:
        """Undo a terminal status the executor imposed on a round, not a run.

        :meth:`WorkflowExecutor.run` finishes -- and marks ``state`` SUCCEEDED
        or FAILED -- the moment its ready-set drains, because for a static,
        fully-declared graph that *is* completion. For a graph still being
        grown one supervisor decision at a time, "nothing left ready in this
        round" means only "wait for the next decision", not "the execution is
        over"; only an explicit ``respond_directly``/``finalize``/``fail``
        decision (handled separately, before the executor ever runs) or the
        turn limit may end a dynamic run.

        The executor already durably checkpointed its (wrong, for this
        purpose) verdict via :class:`CheckpointReason.EXECUTION_FINALIZED`
        before returning, so a crash between that write and this correction
        would otherwise leave the execution looking permanently finished. The
        status is put back to ``RUNNING`` -- bypassing :meth:`~ExecutionState.
        transition_to`'s guard, since terminal statuses are correctly
        unreachable from RUNNING for every case except this one, which the
        transition table cannot know about -- and a fresh, genuinely resumable
        checkpoint is written to overwrite it.
        """
        state.status = ExecutionStatus.RUNNING
        state.completed_at = None
        # A blocking node failure is exactly the shape a RETRY decision needs to
        # see next turn; a stale failure_reason from that spurious verdict must
        # not linger onto whatever status ends the run for real.
        state.failure_reason = None
        await self._events.emit(
            EventType.EXECUTION_RESUMED,
            message="round complete; the supervisor is deciding the next step",
        )
        await self._checkpoint(state, workflow, CheckpointReason.ROUND_COMPLETED, None)

    def _approval_gate(self) -> ApprovalGate:
        return self._approvals.gate()  # type: ignore[return-value]
