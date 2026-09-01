"""Background execution runner: turns a ``POST /executions`` into a live run.

An HTTP request is short-lived; an execution is not. The route handler
persists an execution row and returns immediately, and this runner drives the
actual work as an ``asyncio`` task in this process, tracked so a later request
(cancel, or a GET while it's in flight) can reach it.

This is a single-process design. True cross-process ownership -- a second API
worker resuming an execution the first one is still running -- would need the
engine to poll :class:`~orchestration.coordination.redis.RedisCoordinator`'s
cancellation flag and distributed locks, which exist and are tested but which
nothing in the execution path currently consults. Documented here rather than
half-wired in: a single-process reference deployment is what this runner
actually provides.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestration.agents.runtime import AgentRuntime
from orchestration.budget.meter import BudgetGuard, BudgetMeter
from orchestration.checkpoint.manager import restore_status_for_resume, resume_execution
from orchestration.coordination.redis import RedisEventSink
from orchestration.domain.base import JsonDict
from orchestration.domain.enums import CheckpointReason, ExecutionStatus, PolicyEffect
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Workflow
from orchestration.events.bus import EventBus, ExecutionEventRecorder, InMemoryEventSink
from orchestration.events.sinks import PostgresEventSink
from orchestration.observability.logging import get_logger
from orchestration.persistence.invocation_recorder import InvocationRecorder
from orchestration.policies.approvals import ApprovalService
from orchestration.runtime.orchestrator import MAX_SUPERVISOR_TURNS, ExecutionOrchestrator
from orchestration.supervisor.supervisor import Supervisor
from orchestration.workflow.executor import CancelToken, WorkflowExecutor
from orchestration.workflow.graph import WorkflowGraph

if TYPE_CHECKING:
    from orchestration.api.state import AppState


@dataclass(slots=True)
class RunHandle:
    """Everything a route needs about an execution currently in flight."""

    task: asyncio.Task[None]
    cancel: CancelToken
    state: ExecutionState
    sink: InMemoryEventSink


class ExecutionRunner:
    """Starts, tracks, cancels, and resumes executions running in this process."""

    def __init__(self, app_state: AppState) -> None:
        self._app = app_state
        self._runs: dict[str, RunHandle] = {}

    # -- queries -------------------------------------------------------

    def is_active(self, execution_id: str) -> bool:
        return execution_id in self._runs

    def live_state(self, execution_id: str) -> ExecutionState | None:
        handle = self._runs.get(execution_id)
        return handle.state if handle else None

    # -- lifecycle -------------------------------------------------------

    def start(
        self,
        execution_id: str,
        workflow: Workflow,
        state: ExecutionState,
        *,
        max_turns: int | None,
        event_sequence: int = 0,
    ) -> None:
        """Launch a fresh (or resumed) execution as a background task.

        ``workflow.dynamic`` decides which engine runs it: a hand-authored
        workflow goes straight to :class:`WorkflowExecutor`; one seeded by
        :func:`~orchestration.runtime.orchestrator.seed_dynamic_workflow` goes
        through :class:`ExecutionOrchestrator`, which grows the graph itself.

        ``event_sequence`` continues numbering after a resume rather than
        restarting at zero, so the event log stays gapless and non-colliding
        across a process restart.
        """
        cancel = CancelToken()
        sink = InMemoryEventSink()
        task = asyncio.create_task(
            self._execute(execution_id, workflow, state, cancel, sink, max_turns, event_sequence)
        )
        self._runs[execution_id] = RunHandle(task=task, cancel=cancel, state=state, sink=sink)

    def cancel(self, execution_id: str, *, reason: str = "cancelled by operator") -> bool:
        handle = self._runs.get(execution_id)
        if handle is None:
            return False
        handle.cancel.cancel(reason)
        return True

    async def resume(self, execution_id: str) -> bool:
        """Resume a stranded or approval-paused execution.

        Returns ``False`` without doing anything if it is already running in
        this process -- a double call (a retried approval, say) must not start
        a second concurrent run over the same state.
        """
        if self.is_active(execution_id):
            return False
        context = await resume_execution(self._app.checkpoint_manager, execution_id)
        await restore_status_for_resume(context.state)
        self.start(
            execution_id,
            context.workflow,
            context.state,
            max_turns=None,
            event_sequence=context.event_sequence,
        )
        return True

    async def shutdown(self) -> None:
        """Ask every in-flight execution to stop and wait for it to.

        Cooperative, not forced: a node mid-call still gets to finish its
        current attempt and checkpoint before the task actually ends, which is
        what makes the run resumable after a restart instead of merely killed.
        """
        for handle in self._runs.values():
            handle.cancel.cancel("server shutting down")
        tasks = [h.task for h in self._runs.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- execution ---------------------------------------------------------

    async def _execute(
        self,
        execution_id: str,
        workflow: Workflow,
        state: ExecutionState,
        cancel: CancelToken,
        sink: InMemoryEventSink,
        max_turns: int | None,
        event_sequence: int,
    ) -> None:
        try:
            async with self._app.limiter.execution_slot() as acquired:
                if not acquired:
                    state.transition_to(
                        ExecutionStatus.FAILED,
                        reason="deployment concurrency limit reached; retry later",
                    )
                    await self._app.checkpoint_manager.write(
                        state, workflow, CheckpointReason.EXECUTION_FINALIZED, None
                    )
                    return
                await self._run_engine(
                    execution_id, workflow, state, cancel, sink, max_turns, event_sequence
                )
        except Exception as exc:
            # asyncio.create_task() swallows an exception until someone awaits
            # the task or reads its result; nobody here ever does either, since
            # a route only tracks the RunHandle, not the task's outcome. Without
            # this, a bug anywhere in the engine would leave the execution
            # stuck at whatever status it last reached -- worse than a reported
            # failure, because nothing would ever call it out as broken.
            get_logger(execution_id=execution_id).exception("execution crashed")
            if not state.status.is_terminal:
                state.transition_to(ExecutionStatus.FAILED, reason=f"{type(exc).__name__}: {exc}")
                await self._app.checkpoint_manager.write(
                    state, workflow, CheckpointReason.EXECUTION_FINALIZED, None
                )
        finally:
            self._runs.pop(execution_id, None)

    async def _run_engine(
        self,
        execution_id: str,
        workflow: Workflow,
        state: ExecutionState,
        cancel: CancelToken,
        sink: InMemoryEventSink,
        max_turns: int | None,
        event_sequence: int,
    ) -> None:
        bus = EventBus(
            [
                sink,
                PostgresEventSink(self._app.database),
                RedisEventSink(self._app.redis),
            ],
            start_sequence=event_sequence,
        )
        events = ExecutionEventRecorder(bus=bus, execution_id=execution_id)
        approvals = ApprovalService(self._app.database, events=bus)
        meter = BudgetMeter(
            state.budget, state.budget_usage, elapsed=lambda: state.elapsed_seconds
        )
        invocations = InvocationRecorder(self._app.database)

        async def base_authorise(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            decision = self._app.policy.evaluate(agent_id, tool, arguments)
            if decision.allowed:
                self._app.policy.record_call(agent_id, tool)
            return decision.effect, decision.reason

        runtime = AgentRuntime(
            llm=self._app.llm,
            tools=self._app.tools,
            router=self._app.router,
            authoriser=approvals.tool_authoriser(  # type: ignore[arg-type]
                base_authorise, execution_id=execution_id
            ),
            budget_check=BudgetGuard(meter),
            tool_observer=invocations.record_tool,
        )

        if workflow.dynamic:
            supervisor = Supervisor(
                agents=self._app.agents,
                llm=self._app.llm,
                router=self._app.router,
                tools=self._app.tools,
            )
            orchestrator = ExecutionOrchestrator(
                supervisor=supervisor,
                agents=self._app.agents,
                tools=self._app.tools,
                runtime=runtime,
                approvals=approvals,
                meter=meter,
                events=events,
                checkpoint=self._app.checkpoint_manager.writer(),  # type: ignore[arg-type]
                invocation_recorder=invocations.record_agent,
                cancel_token=cancel,
                sandbox_root=self._app.sandbox_root,
                max_turns=max_turns or MAX_SUPERVISOR_TURNS,
            )
            await orchestrator.run(state, workflow)
            return

        executor = WorkflowExecutor(
            graph=WorkflowGraph(workflow),
            agents=self._app.agents,
            tools=self._app.tools,
            runtime=runtime,
            events=events,
            meter=meter,
            checkpoint=self._app.checkpoint_manager.writer(),  # type: ignore[arg-type]
            invocation_recorder=invocations.record_agent,
            approval_gate=approvals.gate(),  # type: ignore[arg-type]
            cancel_token=cancel,
            sandbox_root=self._app.sandbox_root,
        )
        await executor.run(state)
