"""Workflow execution engine.

Drives a :class:`WorkflowGraph` to completion. One scheduler handles every shape
the design requires -- sequential chains, conditional branches, fan-out, fan-in,
approval gates, retries, timeouts and cancellation -- because they are all the
same question asked repeatedly: *which nodes are ready now?*

The loop:

1. Compute the ready set: nodes whose dependencies are satisfied and whose
   inbound edges are active.
2. Checkpoint before running them.
3. Run the whole ready set concurrently, bounded by a semaphore.
4. Fold results into state, evaluate outgoing conditions, mark unreachable
   branches skipped.
5. Checkpoint after. Repeat.

Properties that took deliberate design rather than falling out:

**Parallelism is implicit.**
    Nothing declares a fan-out. If three nodes are ready, three nodes run. That
    is why fan-out, fan-in and sequential execution need no separate code paths.

**A skipped branch does not stall a join.**
    ``SKIPPED`` counts as complete, so an untaken conditional branch releases its
    downstream join instead of deadlocking it.

**Cancellation is cooperative and immediate.**
    A cancel token is checked between steps and the in-flight ``TaskGroup`` is
    cancelled, so a long agent call is interrupted rather than awaited.

**An approval is a durable pause, not a suspended coroutine.**
    The executor checkpoints, sets ``WAITING_FOR_APPROVAL`` and *returns*. The
    process can be killed at that point and resume will continue correctly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestration.agents.registry import AgentRegistry
from orchestration.agents.runtime import AgentRunContext, AgentRuntime
from orchestration.budget.meter import BudgetMeter
from orchestration.domain.agent import AgentInvocation
from orchestration.domain.base import JsonDict, utc_now
from orchestration.domain.enums import (
    CheckpointReason,
    EventType,
    ExecutionStatus,
    JoinPolicy,
    NodeKind,
    NodeStatus,
)
from orchestration.domain.execution import ExecutionError, ExecutionState
from orchestration.domain.tool import ToolResult
from orchestration.domain.workflow import Workflow, WorkflowNode
from orchestration.errors import (
    ApprovalRejectedError,
    ApprovalRequired,
    BudgetExceededError,
    EngineTimeoutError,
    ExecutionCancelledError,
    NotFoundError,
    OrchestrationError,
    is_retryable,
    to_error_dict,
)
from orchestration.events.bus import ExecutionEventRecorder
from orchestration.observability.metrics import (
    record_budget_exceeded,
    record_execution_finished,
    record_execution_started,
    record_node_execution,
    record_retry,
)
from orchestration.observability.tracing import execution_span, retry_span
from orchestration.tools.base import ToolContext
from orchestration.tools.registry import ToolRegistry
from orchestration.workflow.conditions import (
    evaluate_group,
    explain_group,
    render_template,
)
from orchestration.workflow.graph import WorkflowGraph

#: Persists a checkpoint. Returns nothing; failures are the caller's problem.
CheckpointWriter = Callable[
    [ExecutionState, Workflow, CheckpointReason, str | None], Awaitable[None]
]

#: Records one agent invocation attempt for audit/inspection. Optional and
#: best-effort, like ``CheckpointWriter`` -- an execution's correctness never
#: depends on this succeeding.
AgentInvocationWriter = Callable[[AgentInvocation], Awaitable[None]]

#: Node statuses from which a node may (re-)enter the ready set.
#:
#: ``WAITING_FOR_APPROVAL`` is included because a resumed execution re-runs the
#: node that paused it: that is how the gate gets a chance to read the human
#: decision. Without it the paused node would never be scheduled again and the
#: execution would stall permanently after an approval was granted.
_RUNNABLE_NODE_STATUSES = frozenset({NodeStatus.PENDING, NodeStatus.WAITING_FOR_APPROVAL})

#: Resolves the approval for a node, returning ``(status, approval_id, note)``
#: where status is ``"granted"``, ``"rejected"`` or ``"pending"``.
#:
#: A *gate* rather than a creator: a resumed execution re-runs the node that
#: paused it, so the node must be able to read what a human decided instead of
#: raising again and pausing forever.
ApprovalGate = Callable[[str, str, str], Awaitable[tuple[str, str, str]]]


async def _noop_checkpoint(
    state: ExecutionState,
    workflow: Workflow,
    reason: CheckpointReason,
    node_id: str | None,
) -> None:
    """Default writer: checkpointing is optional for in-memory runs."""


async def _noop_invocation_recorder(invocation: AgentInvocation) -> None:
    """Default writer: invocation recording is optional for in-memory runs."""


class CancelToken:
    """Cooperative cancellation signal.

    A token rather than an ``asyncio.Event`` on the executor because cancellation
    arrives from a different task entirely -- an HTTP request handler -- and may
    need to be shared with a resumed execution in another process via Redis.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""

    def cancel(self, reason: str = "cancelled by operator") -> None:
        self._cancelled = True
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise ExecutionCancelledError(self._reason)


@dataclass(slots=True)
class NodeOutcome:
    """Result of running one node."""

    node_id: str
    status: NodeStatus
    output: JsonDict | None = None
    error: JsonDict | None = None
    confidence: float | None = None
    attempts: int = 1
    duration_seconds: float = 0.0
    approval_required: ApprovalRequired | None = None
    tool_results: tuple[ToolResult, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is NodeStatus.SUCCEEDED


@dataclass(slots=True)
class ExecutionResult:
    """Outcome of one executor run.

    A run can end without the execution ending: an approval pause returns a
    result whose status is ``WAITING_FOR_APPROVAL``, and a later resume continues
    from the checkpoint.
    """

    state: ExecutionState
    workflow: Workflow
    steps: int = 0
    #: Peak count of concurrently running nodes, measured not assumed. The
    #: benchmark's parallelism assertion depends on this being real.
    max_parallelism: int = 0
    #: Nodes that ran per step, for tracing and the parallelism metric.
    step_groups: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def status(self) -> ExecutionStatus:
        return self.state.status

    @property
    def succeeded(self) -> bool:
        return self.state.status is ExecutionStatus.SUCCEEDED

    @property
    def is_paused(self) -> bool:
        return self.state.status is ExecutionStatus.WAITING_FOR_APPROVAL


class WorkflowExecutor:
    """Executes a workflow graph against durable state.

    Args:
        graph: The validated graph.
        agents: Registry for agent node lookup.
        tools: Registry for tool node lookup.
        runtime: Agent runtime.
        events: Event recorder bound to this execution.
        meter: Budget enforcement.
        checkpoint: Persists state. Defaults to a no-op for in-memory runs.
        invocation_recorder: Records each agent attempt for audit/inspection.
            Defaults to a no-op for in-memory runs, same as ``checkpoint``.
        approval_gate: Resolves approvals for approval nodes. Without one, an
            approval node always pauses -- correct for an in-memory run with no
            durable store, since there is nowhere for a decision to live.
        cancel_token: Cooperative cancellation.
        max_concurrent_nodes: Cap on simultaneously running nodes.
        sandbox_root: Filesystem root handed to tools.
        max_steps: Hard ceiling on scheduler iterations, so a pathological graph
            cannot loop forever even if validation missed something.
    """

    def __init__(
        self,
        *,
        graph: WorkflowGraph,
        agents: AgentRegistry,
        tools: ToolRegistry,
        runtime: AgentRuntime,
        events: ExecutionEventRecorder,
        meter: BudgetMeter,
        checkpoint: CheckpointWriter = _noop_checkpoint,
        invocation_recorder: AgentInvocationWriter = _noop_invocation_recorder,
        approval_gate: ApprovalGate | None = None,
        cancel_token: CancelToken | None = None,
        max_concurrent_nodes: int = 8,
        sandbox_root: Path | None = None,
        max_steps: int = 200,
    ) -> None:
        self._graph = graph
        self._agents = agents
        self._tools = tools
        self._runtime = runtime
        self._events = events
        self._meter = meter
        self._checkpoint = checkpoint
        self._invocation_recorder = invocation_recorder
        self._approval_gate = approval_gate
        self._cancel = cancel_token or CancelToken()
        self._semaphore = asyncio.Semaphore(max_concurrent_nodes)
        self._sandbox_root = sandbox_root or Path("./.artifacts")
        self._max_steps = max_steps

    @property
    def workflow(self) -> Workflow:
        return self._graph.workflow

    @property
    def cancel_token(self) -> CancelToken:
        return self._cancel

    # -- main loop ---------------------------------------------------------

    async def run(self, state: ExecutionState) -> ExecutionResult:
        """Execute until completion, pause, or a hard stop.

        Safe to call on a state that has already made progress: the ready-set
        computation derives everything from persisted node statuses, which is what
        makes resume work without a separate code path.
        """
        with execution_span(state.execution_id, state.workflow_id, state.task.description):
            return await self._run_traced(state)

    async def _run_traced(self, state: ExecutionState) -> ExecutionResult:
        result = ExecutionResult(state=state, workflow=self.workflow)
        if state.status is ExecutionStatus.PENDING:
            record_execution_started()

        if state.status is ExecutionStatus.PENDING:
            state.transition_to(ExecutionStatus.RUNNING)
            await self._events.emit(
                EventType.EXECUTION_STARTED,
                message=f"executing workflow {self.workflow.name!r}",
                workflow=self.workflow.name,
                nodes=len(self._graph.nodes),
            )
            await self._checkpoint(state, self.workflow, CheckpointReason.EXECUTION_STARTED, None)
        elif state.status is ExecutionStatus.WAITING_FOR_APPROVAL:
            # Resuming from an approval: the gate is decided, so re-enter RUNNING.
            state.transition_to(ExecutionStatus.RUNNING)
            await self._events.emit(
                EventType.EXECUTION_RESUMED, message="resumed after approval decision"
            )
        elif state.status is ExecutionStatus.RUNNING:
            await self._events.emit(
                EventType.EXECUTION_RESUMED,
                message="resumed an execution left running by an interrupted process",
            )

        try:
            for step in range(1, self._max_steps + 1):
                self._cancel.raise_if_cancelled()
                self._meter.check(f"step:{step}")

                ready = self._ready_nodes(state)
                if not ready:
                    break

                result.steps = step
                result.step_groups.append(tuple(n.id for n in ready))
                result.max_parallelism = max(result.max_parallelism, len(ready))

                state.current_nodes = tuple(n.id for n in ready)
                for node in ready:
                    await self._checkpoint(
                        state, self.workflow, CheckpointReason.BEFORE_NODE, node.id
                    )

                outcomes = await self._run_ready_set(state, ready)

                paused = await self._apply_outcomes(state, outcomes)
                state.current_nodes = ()
                if paused:
                    return result

                if self._should_stop(state):
                    break

            await self._finish(state, result)

        except ExecutionCancelledError as exc:
            await self._handle_cancellation(state, exc)
        except BudgetExceededError as exc:
            await self._handle_budget_exhaustion(state, exc)

        if state.status.is_terminal:
            record_execution_finished(state.status.value, duration_seconds=state.elapsed_seconds)

        return result

    # -- scheduling --------------------------------------------------------

    def _ready_nodes(self, state: ExecutionState) -> list[WorkflowNode]:
        """Nodes that may start now.

        Sorted by id so the ready set is deterministic. Concurrency makes
        completion order vary, but the *scheduling* order must not, or two
        benchmark runs would differ for no reason.
        """
        context = state.evaluation_context()
        active = self._active_edge_ids(state, context)

        ready: list[WorkflowNode] = []
        for node_id, node in sorted(self._graph.nodes.items()):
            node_state = state.node_states.get(node_id)
            if node_state is not None and node_state.status not in _RUNNABLE_NODE_STATUSES:
                continue
            if not self._graph.dependencies_satisfied(node_id, state, active_edges=active):
                continue
            if self._is_unreachable(node_id, state, active):
                continue
            ready.append(node)
        return ready

    def _active_edge_ids(self, state: ExecutionState, context: JsonDict) -> set[str]:
        """Edge ids whose source is complete and whose condition holds.

        An edge from a node that has not finished is neither active nor inactive
        yet -- it is undecided, and treating it as inactive would let a downstream
        node start early.
        """
        active: set[str] = set()
        for edge in self.workflow.edges:
            source_state = state.node_states.get(edge.source)
            if source_state is None or not source_state.is_complete:
                continue
            if evaluate_group(edge.condition, context):
                active.add(edge.id)
        return active

    def _is_unreachable(self, node_id: str, state: ExecutionState, active: set[str]) -> bool:
        """Whether every inbound edge has been decided against this node.

        This is what turns an untaken conditional branch into a ``SKIPPED`` node
        instead of one that waits forever.
        """
        inbound = self._graph.predecessors(node_id)
        if not inbound:
            return False
        for edge in inbound:
            source_state = state.node_states.get(edge.source)
            if source_state is None or not source_state.is_terminal:
                return False  # still undecided
            if edge.id in active:
                return False  # at least one live path in
        return True

    async def _run_ready_set(
        self, state: ExecutionState, ready: list[WorkflowNode]
    ) -> list[NodeOutcome]:
        """Run every ready node concurrently.

        Uses ``gather`` with exceptions returned rather than a ``TaskGroup``: a
        TaskGroup cancels its siblings on the first failure, which is precisely
        the wrong behaviour for a fan-out whose join tolerates partial results.
        Cancellation is handled explicitly instead, where the semantics are a
        deliberate choice rather than a side effect.
        """
        tasks = [asyncio.create_task(self._run_node(state, node)) for node in ready]
        try:
            settled = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        outcomes: list[NodeOutcome] = []
        for node, result in zip(ready, settled, strict=True):
            if isinstance(result, ExecutionCancelledError | BudgetExceededError):
                # Execution-level stops, not node failures. Converting these into
                # a FAILED node would report "a node failed" for what is really
                # "the run hit its ceiling", and lose the dimension that tripped.
                raise result
            if isinstance(result, BaseException):
                outcomes.append(
                    NodeOutcome(
                        node_id=node.id,
                        status=NodeStatus.FAILED,
                        error=to_error_dict(result),
                    )
                )
            else:
                outcomes.append(result)
        return outcomes

    # -- node execution ----------------------------------------------------

    async def _run_node(self, state: ExecutionState, node: WorkflowNode) -> NodeOutcome:
        """Run one node with its retry policy applied."""
        async with self._semaphore:
            self._cancel.raise_if_cancelled()
            node_state = state.node_state(node.id)
            policy = node.retry_policy
            started = time.perf_counter()

            attempt = node_state.attempts
            while True:
                attempt += 1
                node_state.mark_running()
                await self._events.emit(
                    EventType.NODE_STARTED,
                    node_id=node.id,
                    agent_id=node.agent_id,
                    tool=node.tool,
                    message=f"running {node.kind.value} node {node.id!r} (attempt {attempt})",
                    attempt=attempt,
                    kind=node.kind.value,
                )
                try:
                    outcome = await self._dispatch(state, node, attempt)
                    outcome.attempts = attempt
                    outcome.duration_seconds = round(time.perf_counter() - started, 6)
                    return outcome

                except ApprovalRequired as pending:
                    return NodeOutcome(
                        node_id=node.id,
                        status=NodeStatus.WAITING_FOR_APPROVAL,
                        approval_required=pending,
                        attempts=attempt,
                    )

                except (BudgetExceededError, ExecutionCancelledError):
                    # Both are execution-level stops, not node failures; they
                    # must reach the main loop rather than be retried.
                    raise

                except Exception as exc:
                    error = to_error_dict(exc)
                    retryable = is_retryable(exc)
                    state.record_error(
                        ExecutionError(
                            node_id=node.id,
                            agent_id=node.agent_id,
                            tool=node.tool,
                            code=str(error["code"]),
                            message=str(error["message"]),
                            retryable=retryable,
                            attempt=attempt,
                        )
                    )
                    if not policy.should_retry(attempt, exc):
                        await self._events.emit(
                            EventType.RETRY_EXHAUSTED if retryable else EventType.NODE_FAILED,
                            node_id=node.id,
                            agent_id=node.agent_id,
                            message=f"node {node.id!r} failed: {error['message']}",
                            attempts=attempt,
                            **{"error_code": error["code"], "retryable": retryable},
                        )
                        return NodeOutcome(
                            node_id=node.id,
                            status=NodeStatus.FAILED,
                            error=error,
                            attempts=attempt,
                            duration_seconds=round(time.perf_counter() - started, 6),
                        )

                    delay = policy.backoff_for(attempt, exc)
                    state.record_retry(node.id)
                    self._meter.record_retry()
                    record_retry(node.kind.value, str(error["code"]))
                    await self._events.emit(
                        EventType.RETRY_STARTED,
                        node_id=node.id,
                        agent_id=node.agent_id,
                        message=(
                            f"retrying {node.id!r} after {error['code']} "
                            f"(attempt {attempt + 1}, waiting {delay}s)"
                        ),
                        attempt=attempt + 1,
                        backoff_seconds=delay,
                        error_code=error["code"],
                    )
                    if delay > 0:
                        with retry_span(
                            state.execution_id,
                            node.id,
                            attempt=attempt,
                            error_code=str(error["code"]),
                        ):
                            await asyncio.sleep(delay)
                    self._cancel.raise_if_cancelled()

    async def _dispatch(
        self, state: ExecutionState, node: WorkflowNode, attempt: int
    ) -> NodeOutcome:
        """Route to the handler for this node kind."""
        timeout = node.timeout_seconds
        coroutine = self._handler_for(state, node, attempt)
        if timeout is None:
            return await coroutine
        try:
            async with asyncio.timeout(timeout):
                return await coroutine
        except TimeoutError as exc:
            raise EngineTimeoutError(
                f"node {node.id!r} exceeded its {timeout}s timeout",
                node_id=node.id,
                timeout_seconds=timeout,
            ) from exc

    def _handler_for(
        self, state: ExecutionState, node: WorkflowNode, attempt: int
    ) -> Awaitable[NodeOutcome]:
        match node.kind:
            case NodeKind.AGENT:
                return self._run_agent_node(state, node, attempt)
            case NodeKind.TOOL:
                return self._run_tool_node(state, node, attempt)
            case NodeKind.JOIN:
                return self._run_join_node(state, node)
            case NodeKind.BRANCH:
                return self._run_branch_node(state, node)
            case NodeKind.APPROVAL:
                return self._run_approval_node(state, node)
            case NodeKind.TERMINAL:
                return self._run_terminal_node(state, node)
            case NodeKind.SUPERVISOR:
                # A supervisor node inside a static workflow is a no-op marker:
                # dynamic routing is driven by the orchestrator, not from inside
                # the graph. Treated as a pass-through so a diagram may show it.
                return self._run_passthrough_node(node)

    async def _run_agent_node(
        self, state: ExecutionState, node: WorkflowNode, attempt: int
    ) -> NodeOutcome:
        assert node.agent_id is not None
        definition = self._agents.try_get(node.agent_id)
        if definition is None:
            raise NotFoundError(
                f"node {node.id!r} references unregistered agent {node.agent_id!r}",
                node_id=node.id,
                agent=node.agent_id,
            )

        context = state.evaluation_context()
        instruction = (
            render_template(node.input_template, context)
            if node.input_template
            else state.task.description
        )

        self._meter.check(f"agent:{node.agent_id}")
        run_context = AgentRunContext(
            execution_id=state.execution_id,
            node_id=node.id,
            instruction=instruction,
            prior_outputs=self._upstream_outputs(state, node.id),
            variables=dict(state.variables),
            sandbox_root=self._sandbox_root,
            deadline_seconds=node.timeout_seconds or definition.timeout_seconds,
            attempt=attempt,
            trace_id=state.trace_id,
            seed=f"{state.execution_id}:{node.id}",
        )

        await self._events.emit(
            EventType.AGENT_INVOKED,
            node_id=node.id,
            agent_id=node.agent_id,
            message=f"invoking {node.agent_id!r}",
            attempt=attempt,
        )
        result = await self._runtime.run(definition, run_context)
        await self._invocation_recorder(result.invocation)

        if result.pending_approval is not None:
            raise result.pending_approval

        self._meter.record_agent_step()
        self._meter.record_llm_usage(
            input_tokens=result.invocation.input_tokens,
            output_tokens=result.invocation.output_tokens,
            cost_usd=result.invocation.cost_usd,
        )
        self._meter.record_tool_call(result.invocation.tool_calls)

        if result.output is None:
            error = result.invocation.error or {
                "code": "agent_failed",
                "message": "agent produced no output",
            }
            raise OrchestrationError(str(error.get("message", "agent produced no output")))

        await self._events.emit(
            EventType.AGENT_COMPLETED,
            node_id=node.id,
            agent_id=node.agent_id,
            message=f"{node.agent_id!r} completed with confidence {result.output.confidence}",
            confidence=result.output.confidence,
            tokens=result.invocation.total_tokens,
            cost_usd=result.invocation.cost_usd,
            tool_calls=result.invocation.tool_calls,
        )

        return NodeOutcome(
            node_id=node.id,
            status=NodeStatus.SUCCEEDED,
            output=result.output.model_dump(mode="json"),
            confidence=result.output.confidence,
            tool_results=result.tool_results,
        )

    async def _run_tool_node(
        self, state: ExecutionState, node: WorkflowNode, attempt: int
    ) -> NodeOutcome:
        """Run a tool directly, with no agent in the loop.

        Still budget-checked and still passes through the tool's own argument
        validation -- a tool node is not a way to bypass either.
        """
        assert node.tool is not None
        self._meter.check(f"tool:{node.tool}")
        tool = self._tools.get(node.tool)

        context = state.evaluation_context()
        arguments = {
            key: render_template(value, context) if isinstance(value, str) else value
            for key, value in node.tool_arguments.items()
        }

        await self._events.emit(
            EventType.TOOL_INVOKED,
            node_id=node.id,
            tool=node.tool,
            message=f"invoking tool {node.tool!r}",
            attempt=attempt,
        )
        output = await tool.invoke(
            arguments,
            ToolContext(
                execution_id=state.execution_id,
                node_id=node.id,
                sandbox_root=self._sandbox_root,
                deadline_seconds=node.timeout_seconds or tool.spec.timeout_seconds,
                attempt=attempt,
            ),
        )
        self._meter.record_tool_call()
        await self._events.emit(
            EventType.TOOL_COMPLETED,
            node_id=node.id,
            tool=node.tool,
            message=f"tool {node.tool!r} completed",
        )
        return NodeOutcome(node_id=node.id, status=NodeStatus.SUCCEEDED, output=output)

    async def _run_join_node(self, state: ExecutionState, node: WorkflowNode) -> NodeOutcome:
        """Fold upstream results into one output.

        Reached only once its policy is satisfied, so the work here is collection
        rather than waiting. Partial results are preserved and labelled: an
        ``ALL_SETTLED`` join that lost one branch must hand the analyst what did
        succeed, plus the knowledge that something failed.
        """
        upstream = self._graph.predecessors(node.id)
        succeeded: dict[str, JsonDict] = {}
        failed: dict[str, JsonDict] = {}

        for edge in upstream:
            node_state = state.node_states.get(edge.source)
            if node_state is None:
                continue
            if node_state.status is NodeStatus.SUCCEEDED:
                succeeded[edge.source] = state.agent_outputs.get(edge.source, {})
            elif node_state.status is NodeStatus.FAILED:
                failed[edge.source] = node_state.error or {}

        state.node_state(node.id).satisfied_by = tuple(sorted(succeeded))

        await self._events.emit(
            EventType.NODE_COMPLETED,
            node_id=node.id,
            message=(
                f"join {node.id!r} ({node.join_policy.value}) collected "
                f"{len(succeeded)} succeeded, {len(failed)} failed"
            ),
            policy=node.join_policy.value,
            succeeded=sorted(succeeded),
            failed=sorted(failed),
        )

        if node.join_policy is JoinPolicy.ANY:
            await self._cancel_losing_branches(state, node, succeeded)

        return NodeOutcome(
            node_id=node.id,
            status=NodeStatus.SUCCEEDED,
            output={
                "joined": succeeded,
                "failed_branches": failed,
                "policy": node.join_policy.value,
                "partial": bool(failed),
            },
        )

    async def _cancel_losing_branches(
        self, state: ExecutionState, node: WorkflowNode, winners: dict[str, JsonDict]
    ) -> None:
        """Mark still-pending siblings of an ``ANY`` join as cancelled.

        Without this the losing branches would be scheduled after the join has
        already fired, spending budget on work whose result is discarded.
        """
        for edge in self._graph.predecessors(node.id):
            if edge.source in winners:
                continue
            sibling = state.node_state(edge.source)
            if sibling.status in {NodeStatus.PENDING, NodeStatus.READY}:
                sibling.status = NodeStatus.CANCELLED
                sibling.completed_at = utc_now()
                await self._events.emit(
                    EventType.NODE_SKIPPED,
                    node_id=edge.source,
                    message=f"cancelled: join {node.id!r} was satisfied by another branch",
                )

    async def _run_branch_node(self, state: ExecutionState, node: WorkflowNode) -> NodeOutcome:
        """Evaluate outgoing conditions and record which paths are live.

        The branch node itself does nothing but decide; edge activation is what
        the scheduler actually consumes. Recording the explanation makes "why did
        it take that path" answerable from the event log.
        """
        context = state.evaluation_context()
        decisions = {
            edge.target: explain_group(edge.condition, context)
            for edge in self._graph.successors(node.id)
        }
        taken = [
            edge.target
            for edge in self._graph.successors(node.id)
            if evaluate_group(edge.condition, context)
        ]
        await self._events.emit(
            EventType.NODE_COMPLETED,
            node_id=node.id,
            message=f"branch {node.id!r} selected: {', '.join(taken) or 'no path'}",
            taken=taken,
            evaluations=decisions,
        )
        return NodeOutcome(
            node_id=node.id,
            status=NodeStatus.SUCCEEDED,
            output={"taken": taken, "evaluations": decisions},
        )

    async def _run_approval_node(self, state: ExecutionState, node: WorkflowNode) -> NodeOutcome:
        """Consult the approval gate, and pause durably if undecided.

        Deliberately never awaits a decision. Awaiting would tie the pause to this
        process's lifetime, and the point of the design is that it survives a
        restart -- so the node returns control to the main loop, which checkpoints
        and stops. When the execution is later resumed, this same node runs again
        and the gate reports whatever a human decided in the meantime.
        """
        reason = node.approval_reason or f"workflow node {node.id!r} requires human approval"

        if self._approval_gate is None:
            # No durable store: there is nowhere for a decision to live, so the
            # only honest behaviour is to pause and stay paused.
            raise ApprovalRequired(reason, approval_id="", node_id=node.id)

        status, approval_id, note = await self._approval_gate(state.execution_id, node.id, reason)

        if status == "granted":
            await self._events.emit(
                EventType.APPROVAL_GRANTED,
                node_id=node.id,
                message=f"approval {approval_id} granted; proceeding",
                approval_id=approval_id,
            )
            state.pending_approval_id = None
            return NodeOutcome(
                node_id=node.id,
                status=NodeStatus.SUCCEEDED,
                output={"approved": True, "approval_id": approval_id, "note": note},
            )

        if status == "rejected":
            await self._events.emit(
                EventType.APPROVAL_REJECTED,
                node_id=node.id,
                message=f"approval {approval_id} was refused: {note}",
                approval_id=approval_id,
            )
            # Terminal, not retryable: re-asking a reviewer who said no is not a
            # recovery strategy.
            raise ApprovalRejectedError(
                f"node {node.id!r} was not approved: {note}",
                approval_id=approval_id,
                node_id=node.id,
            )

        raise ApprovalRequired(reason, approval_id=approval_id, node_id=node.id)

    async def _run_terminal_node(self, state: ExecutionState, node: WorkflowNode) -> NodeOutcome:
        """Record the final output from the most recent upstream result."""
        upstream = self._graph.predecessors(node.id)
        final: JsonDict = {}
        for edge in upstream:
            payload = state.agent_outputs.get(edge.source)
            if payload:
                final = payload
        if final.get("content"):
            state.final_output = str(final["content"])
        return NodeOutcome(node_id=node.id, status=NodeStatus.SUCCEEDED, output=final)

    async def _run_passthrough_node(self, node: WorkflowNode) -> NodeOutcome:
        return NodeOutcome(node_id=node.id, status=NodeStatus.SUCCEEDED, output={})

    # -- result folding ----------------------------------------------------

    async def _apply_outcomes(self, state: ExecutionState, outcomes: list[NodeOutcome]) -> bool:
        """Fold outcomes into state. Returns ``True`` if the run must pause."""
        paused = False

        for outcome in outcomes:
            node = self._graph.nodes[outcome.node_id]
            node_state = state.node_state(outcome.node_id)

            if outcome.status is NodeStatus.WAITING_FOR_APPROVAL:
                assert outcome.approval_required is not None
                node_state.status = NodeStatus.WAITING_FOR_APPROVAL
                node_state.approval_id = outcome.approval_required.approval_id or None
                state.pending_approval_id = node_state.approval_id
                await self._events.emit(
                    EventType.APPROVAL_REQUESTED,
                    node_id=outcome.node_id,
                    message=str(outcome.approval_required),
                    approval_id=node_state.approval_id,
                )
                # Transition *then* checkpoint. Checkpointing first would persist
                # the pause with status RUNNING, so an operator (and the API)
                # would see a running execution that is actually parked waiting
                # for a human -- the same defect as failing to persist a terminal
                # status, and equally misleading.
                state.transition_to(ExecutionStatus.WAITING_FOR_APPROVAL)
                await self._checkpoint(
                    state, self.workflow, CheckpointReason.BEFORE_APPROVAL, outcome.node_id
                )
                paused = True
                continue

            if outcome.succeeded:
                node_state.mark_succeeded(confidence=outcome.confidence)
                record_node_execution(node.kind.value, "succeeded")
                if outcome.output is not None:
                    state.record_agent_output(
                        outcome.node_id, outcome.output, output_key=node.output_key
                    )
                if node_state.attempts > 1:
                    # A node that needed retries and then succeeded is a recovery,
                    # recorded explicitly because recovery rate is a headline
                    # metric rather than something to infer later.
                    state.mark_last_error_recovered(outcome.node_id)
                await self._events.emit(
                    EventType.NODE_COMPLETED,
                    node_id=outcome.node_id,
                    agent_id=node.agent_id,
                    message=f"node {outcome.node_id!r} succeeded",
                    attempts=outcome.attempts,
                    duration_seconds=outcome.duration_seconds,
                    confidence=outcome.confidence,
                )
                await self._checkpoint(
                    state, self.workflow, CheckpointReason.AFTER_NODE_SUCCESS, outcome.node_id
                )
            else:
                node_state.mark_failed(outcome.error or {"code": "unknown", "message": ""})
                record_node_execution(node.kind.value, "failed")
                await self._checkpoint(
                    state, self.workflow, CheckpointReason.AFTER_NODE_FAILURE, outcome.node_id
                )

        await self._skip_unreachable(state)
        return paused

    async def _skip_unreachable(self, state: ExecutionState) -> None:
        """Mark nodes whose every inbound path is dead as skipped.

        Done after each step rather than at the end: a downstream join needs its
        untaken branches marked complete *before* it is evaluated, or it waits on
        work that will never happen.
        """
        context = state.evaluation_context()
        active = self._active_edge_ids(state, context)
        for node_id in sorted(self._graph.nodes):
            node_state = state.node_states.get(node_id)
            if node_state is not None and node_state.status is not NodeStatus.PENDING:
                continue
            if self._is_unreachable(node_id, state, active):
                state.node_state(node_id).mark_skipped("no active inbound path")
                await self._events.emit(
                    EventType.NODE_SKIPPED,
                    node_id=node_id,
                    message=f"skipped {node_id!r}: every inbound branch was not taken",
                )

    def _should_stop(self, state: ExecutionState) -> bool:
        """Whether a node failure should end the execution.

        A failure stops the run only when nothing downstream can still proceed.
        An optional node, or one whose join tolerates failures, must not abort
        the whole workflow -- that is the difference between partial results and
        no results.
        """
        return any(
            self._is_blocking_failure(node_id)
            for node_id, node_state in state.node_states.items()
            if node_state.status is NodeStatus.FAILED
        )

    def _is_blocking_failure(self, node_id: str) -> bool:
        """Whether a failed node should end the execution.

        The single definition, used both mid-run by :meth:`_should_stop` and at
        completion by :meth:`_finish`. Having two notions of "does this failure
        matter" was a real bug: a failure tolerated mid-run by a downstream
        ``ALL_SETTLED`` join would let execution continue, and then fail the run
        anyway at the end.
        """
        node = self._graph.nodes.get(node_id)
        if node is not None and node.optional:
            return False
        return not self._has_tolerant_downstream(node_id)

    def _has_tolerant_downstream(self, node_id: str) -> bool:
        """Whether a failure is absorbed by a downstream tolerant join."""
        frontier = [edge.target for edge in self._graph.successors(node_id)]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self._graph.nodes.get(current)
            if node is None:
                continue
            if node.kind is NodeKind.JOIN and node.join_policy in {
                JoinPolicy.ALL_SETTLED,
                JoinPolicy.ANY,
                JoinPolicy.QUORUM,
            }:
                return True
            frontier.extend(edge.target for edge in self._graph.successors(current))
        return False

    # -- termination -------------------------------------------------------

    async def _finish(self, state: ExecutionState, result: ExecutionResult) -> None:
        """Set the final status once no nodes remain runnable."""
        await self._checkpoint(state, self.workflow, CheckpointReason.BEFORE_FINALIZATION, None)

        blocking = sorted(
            node_id for node_id in state.failed_node_ids() if self._is_blocking_failure(node_id)
        )

        if blocking:
            state.transition_to(
                ExecutionStatus.FAILED,
                reason=f"node(s) failed without recovery: {', '.join(blocking)}",
            )
            await self._events.emit(
                EventType.EXECUTION_FAILED,
                message=state.failure_reason or "execution failed",
                failed_nodes=blocking,
                steps=result.steps,
            )
            # Persist the terminal status. Without this the stored state would
            # still read RUNNING, and resume would treat a finished execution as
            # resumable and redo its finalisation.
            await self._checkpoint(state, self.workflow, CheckpointReason.EXECUTION_FINALIZED, None)
            return

        if state.final_output is None:
            state.final_output = self._derive_final_output(state)

        state.transition_to(ExecutionStatus.SUCCEEDED)
        await self._events.emit(
            EventType.EXECUTION_COMPLETED,
            message=f"execution completed in {result.steps} step(s)",
            steps=result.steps,
            max_parallelism=result.max_parallelism,
            nodes_succeeded=len(state.succeeded_node_ids()),
            cost_usd=round(state.budget_usage.cost_usd, 6),
            total_tokens=state.budget_usage.total_tokens,
        )
        # As above: the success must be durable, or a crash here leaves the run
        # looking resumable when it is actually complete.
        await self._checkpoint(state, self.workflow, CheckpointReason.EXECUTION_FINALIZED, None)

    def _derive_final_output(self, state: ExecutionState) -> str:
        """Choose a final answer when no terminal node set one.

        Prefers the output of the deepest succeeded node -- the furthest point
        reached in the graph is the most synthesised result available.
        """
        succeeded = [n for n in state.succeeded_node_ids() if n in self._graph.nodes]
        if not succeeded:
            return ""
        deepest = max(succeeded, key=lambda n: (self._graph.depth_of(n), n))
        payload = state.agent_outputs.get(deepest, {})
        return str(payload.get("content", "")) if isinstance(payload, dict) else str(payload)

    async def _handle_cancellation(
        self, state: ExecutionState, exc: ExecutionCancelledError
    ) -> None:
        state.transition_to(ExecutionStatus.CANCELLED, reason=exc.message)
        for node_id, node_state in state.node_states.items():
            if node_state.status is NodeStatus.RUNNING:
                node_state.status = NodeStatus.CANCELLED
                node_state.completed_at = utc_now()
                await self._events.emit(
                    EventType.NODE_SKIPPED,
                    node_id=node_id,
                    message="cancelled while running",
                )
        await self._events.emit(
            EventType.EXECUTION_CANCELLED, message=exc.message, reason=exc.message
        )
        await self._checkpoint(state, self.workflow, CheckpointReason.ON_CANCELLATION, None)

    async def _handle_budget_exhaustion(
        self, state: ExecutionState, exc: BudgetExceededError
    ) -> None:
        state.transition_to(
            ExecutionStatus.BUDGET_EXCEEDED,
            reason=f"{exc.dimension} limit of {exc.limit} reached (used {exc.used})",
        )
        await self._events.emit(
            EventType.BUDGET_EXCEEDED,
            message=state.failure_reason or "budget exceeded",
            dimension=exc.dimension,
            limit=exc.limit,
            used=exc.used,
        )
        record_budget_exceeded(exc.dimension)
        await self._checkpoint(state, self.workflow, CheckpointReason.ON_BUDGET_EXCEEDED, None)

    # -- helpers -----------------------------------------------------------

    def _upstream_outputs(self, state: ExecutionState, node_id: str) -> dict[str, JsonDict]:
        """Outputs of this node's immediate predecessors.

        Immediate rather than transitive: a join already aggregates its inputs, so
        walking the whole ancestry would duplicate content and inflate the prompt.
        """
        outputs: dict[str, JsonDict] = {}
        for edge in self._graph.predecessors(node_id):
            payload = state.agent_outputs.get(edge.source)
            if not payload:
                continue
            joined = payload.get("joined") if isinstance(payload, dict) else None
            if isinstance(joined, dict):
                outputs.update(joined)
            else:
                outputs[edge.source] = payload
        return outputs


@contextlib.asynccontextmanager
async def cancellable(token: CancelToken):  # type: ignore[no-untyped-def]
    """Context manager that cancels ``token`` on exit.

    Used by the API's cancel endpoint and by tests that need to interrupt a run
    from outside it.
    """
    try:
        yield token
    finally:
        token.cancel("scope exited")
