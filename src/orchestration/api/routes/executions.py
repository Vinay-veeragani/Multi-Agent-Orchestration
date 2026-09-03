"""``/executions`` -- creating, inspecting, and steering executions.

Creation returns as soon as the run is durably queued; the run itself happens
in the background via :class:`~orchestration.api.runner.ExecutionRunner`. Every
other route here is a thin read or a signal into that runner.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from orchestration.api.schemas import (
    ApprovalDecisionRequest,
    CancelRequest,
    CreateExecutionRequest,
    ExecutionAccepted,
)
from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.domain.approval import ApprovalRequest
from orchestration.domain.base import JsonDict, new_id
from orchestration.domain.budget import Budget
from orchestration.domain.enums import EventType, ExecutionStatus
from orchestration.domain.events import EventFilter, ExecutionEvent
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task, Workflow
from orchestration.errors import InputValidationError, NotFoundError
from orchestration.persistence.repositories import (
    EventRepository,
    ExecutionRepository,
    InvocationRepository,
    WorkflowRepository,
)
from orchestration.policies.approvals import ApprovalService
from orchestration.runtime.orchestrator import seed_dynamic_workflow

_TERMINAL_STREAM_EVENT_VALUES = {
    EventType.EXECUTION_COMPLETED.value,
    EventType.EXECUTION_FAILED.value,
    EventType.EXECUTION_CANCELLED.value,
}

router = APIRouter(
    prefix="/executions", tags=["executions"], dependencies=[Depends(require_api_key)]
)


def _requested_budget(request: CreateExecutionRequest, settings_budget: Budget) -> Budget:
    """Tighten the deployment default against whatever the caller asked for.

    ``tightened_to`` is element-wise-stricter, so a request can only narrow the
    deployment's ceiling, never widen it -- the same rule an agent-level budget
    is held to.
    """
    requested = Budget(
        max_cost_usd=request.max_cost_usd,
        max_tokens=request.max_tokens,
        max_duration_seconds=request.max_duration_seconds,
        max_agent_steps=request.max_agent_steps,
        max_tool_calls=None,
    )
    return requested.tightened_to(settings_budget)


@router.get("")
async def list_executions(
    limit: int = 50,
    status_filter: ExecutionStatus | None = None,
    app_state: AppState = Depends(get_app_state),
) -> list[JsonDict]:
    """Recent executions, newest first -- a dashboard's listing source.

    Summaries only (id, workflow, status, task, cost, timestamps), from
    :meth:`ExecutionRepository.list_recent`, which existed unused before this
    route -- the CLI and every other caller so far has only ever needed one
    execution by id at a time. Reads the durable table directly rather than
    ``ExecutionRunner``'s in-memory state, so a completed run still shows up
    correctly here after the process that ran it has moved on.
    """
    async with app_state.database.session() as session:
        return await ExecutionRepository(session).list_recent(limit=limit, status=status_filter)


@router.post("", response_model=ExecutionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_execution(
    request: CreateExecutionRequest, app_state: AppState = Depends(get_app_state)
) -> ExecutionAccepted:
    settings = app_state.settings
    budget = _requested_budget(
        request,
        Budget(
            max_cost_usd=settings.budget_max_cost_usd,
            max_tokens=settings.budget_max_tokens,
            max_duration_seconds=settings.budget_max_duration_seconds,
            max_agent_steps=settings.budget_max_agent_steps,
            max_tool_calls=settings.budget_max_tool_calls,
            max_retries=settings.budget_max_retries,
        ),
    )

    if request.workflow_id is not None:
        async with app_state.database.session() as session:
            workflow = await WorkflowRepository(session).get(request.workflow_id)
    else:
        workflow = seed_dynamic_workflow()
        async with app_state.database.session() as session:
            await WorkflowRepository(session).save(workflow)

    candidate_id = new_id("execution")
    async with app_state.database.session() as session:
        execution_id = await ExecutionRepository(session).create(
            execution_id=candidate_id,
            workflow_id=workflow.id,
            task_description=request.task,
            idempotency_key=request.idempotency_key,
        )

    if execution_id != candidate_id:
        # idempotency_key collided with an already-created execution: this is a
        # retried request, not a new one. Report what that execution is
        # actually doing rather than starting a second run over its state.
        existing = await _load_state(execution_id, app_state)
        return ExecutionAccepted(
            execution_id=execution_id,
            workflow_id=existing.workflow_id,
            status=existing.status.value,
        )

    state = ExecutionState(
        execution_id=execution_id,
        workflow_id=workflow.id,
        task=Task(description=request.task, success_criteria=request.success_criteria),
        budget=budget,
    )
    # Durable before the background task is even scheduled, so a crash between
    # here and the run's first checkpoint still leaves something to GET and
    # something for an operator's resume sweep to find.
    await app_state.checkpoint_manager.save_state(state, workflow, check_version=False)

    app_state.runner.start(execution_id, workflow, state, max_turns=request.max_turns)
    return ExecutionAccepted(
        execution_id=execution_id, workflow_id=workflow.id, status=state.status.value
    )


async def _load_state(execution_id: str, app_state: AppState) -> ExecutionState:
    """The freshest view of an execution's state.

    An execution running in this process has a live, in-memory
    :class:`ExecutionState` the engine is actively mutating -- returning that
    is more current than anything on disk, which only reflects the last
    checkpoint. Anything not currently running here (finished, or owned by a
    different process) falls back to the durable copy.
    """
    live = app_state.runner.live_state(execution_id)
    if live is not None:
        return live
    state, _workflow = await app_state.checkpoint_manager.load_state(execution_id)
    return state


@router.get("/{execution_id}", response_model=ExecutionState)
async def get_execution(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> ExecutionState:
    return await _load_state(execution_id, app_state)


@router.get("/{execution_id}/workflow", response_model=Workflow)
async def get_execution_workflow(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> Workflow:
    """The graph as this execution actually ran it, not as first registered.

    A dynamic execution's supervisor grows its workflow turn by turn via
    ``Workflow.extended_with`` (see ``orchestration.runtime.orchestrator``);
    that growth is never written back to the `workflows` table, only into the
    execution's own checkpointed state. ``GET /workflows/{id}`` would therefore
    return just the single-node seed for any dynamic execution -- this route
    reads the same state row `_load_state` does, so a live or finished
    execution's graph view always reflects its real topology.
    """
    _state, workflow = await app_state.checkpoint_manager.load_state(execution_id)
    return workflow


@router.post("/{execution_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_execution(
    execution_id: str,
    request: CancelRequest = CancelRequest(),
    app_state: AppState = Depends(get_app_state),
) -> JsonDict:
    if app_state.runner.cancel(execution_id, reason=request.reason):
        return {"execution_id": execution_id, "cancellation": "requested"}
    raise NotFoundError(
        f"execution {execution_id!r} is not running in this process",
        execution=execution_id,
        hint="only an execution currently in flight can be cancelled",
    )


@router.post("/{execution_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_execution_route(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> JsonDict:
    """Resume an execution stranded by a crashed or restarted process.

    A no-op, not an error, when the execution is already running in this
    process -- an operator re-issuing the same resume request (or a resume
    sweep racing a request that already succeeded) must not start a second
    concurrent run over the same state.
    """
    if await app_state.runner.resume(execution_id):
        return {"execution_id": execution_id, "resume": "started"}
    return {"execution_id": execution_id, "resume": "already running in this process"}


async def _resolve_pending_approval(
    execution_id: str, approval_id: str | None, service: ApprovalService
) -> ApprovalRequest:
    if approval_id is not None:
        return await service.get(approval_id)
    pending = await service.pending_for(execution_id)
    if not pending:
        raise NotFoundError(
            f"execution {execution_id!r} has no pending approval", execution=execution_id
        )
    if len(pending) > 1:
        raise InputValidationError(
            f"execution {execution_id!r} has {len(pending)} pending approvals; "
            "specify approval_id",
            execution=execution_id,
            pending_ids=[p.id for p in pending],
        )
    return pending[0]


@router.get("/{execution_id}/approvals", response_model=list[ApprovalRequest])
async def list_pending_approvals(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> list[ApprovalRequest]:
    """What a human is actually being asked to decide, in full.

    A HITL UI needs the ``action``/``risk_reason``/``parameters`` this
    returns before it can render an approve/reject prompt -- ``approve``/
    ``reject`` themselves only accept a decision, they were never meant to
    double as a way to read what's pending.
    """
    service = ApprovalService(app_state.database)
    return await service.pending_for(execution_id)


@router.post("/{execution_id}/approve", response_model=ApprovalRequest)
async def approve_execution(
    execution_id: str,
    request: ApprovalDecisionRequest,
    app_state: AppState = Depends(get_app_state),
) -> ApprovalRequest:
    service = ApprovalService(app_state.database)
    target = await _resolve_pending_approval(execution_id, request.approval_id, service)
    decided = await service.approve(
        target.id, by=request.by, note=request.note, modified_arguments=request.modified_arguments
    )
    await app_state.runner.resume(execution_id)
    return decided


@router.post("/{execution_id}/reject", response_model=ApprovalRequest)
async def reject_execution(
    execution_id: str,
    request: ApprovalDecisionRequest,
    app_state: AppState = Depends(get_app_state),
) -> ApprovalRequest:
    service = ApprovalService(app_state.database)
    target = await _resolve_pending_approval(execution_id, request.approval_id, service)
    decided = await service.reject(target.id, by=request.by, note=request.note)
    await app_state.runner.resume(execution_id)
    return decided


@router.get("/{execution_id}/events", response_model=list[ExecutionEvent])
async def get_execution_events(
    execution_id: str,
    after_sequence: int | None = None,
    limit: int = 200,
    app_state: AppState = Depends(get_app_state),
) -> list[ExecutionEvent]:
    event_filter = EventFilter(after_sequence=after_sequence, limit=limit)
    async with app_state.database.session() as session:
        return await EventRepository(session).query(execution_id, event_filter)


@router.get("/{execution_id}/agent-invocations")
async def get_agent_invocations(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> list[JsonDict]:
    """Every agent attempt this execution made -- model, tokens, cost, result.

    Reads a table (`agent_invocations`) the engine already writes to on
    every attempt via `InvocationRepository.record_agent` -- this is the
    first route to read it back; nothing about how it's written changes.
    """
    async with app_state.database.session() as session:
        return await InvocationRepository(session).agent_invocations(execution_id)


@router.get("/{execution_id}/tool-invocations")
async def get_tool_invocations(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> list[JsonDict]:
    """Every tool call this execution made -- which tool, policy effect, status.

    Same story as agent invocations: `tool_invocations` is already written
    on every call (it's also how idempotent resume works, via the claimed
    idempotency key); this is the first route to expose it for inspection.
    Arguments and results are deliberately omitted here -- they can contain
    whatever an agent passed a tool, unfiltered by this endpoint, and a tool
    inspector showing raw call payloads needs its own review before that's
    safe to expose over HTTP.
    """
    async with app_state.database.session() as session:
        return await InvocationRepository(session).tool_invocations(execution_id)


def _format_sse(entry: dict[str, str]) -> str:
    """Render one Redis stream entry as one SSE message.

    The stream stores every field as a string (Redis' own constraint) and
    ``payload`` is itself already JSON-encoded there (see
    :meth:`~orchestration.domain.events.ExecutionEvent.to_stream_fields`); it
    is decoded once here so the client receives one clean JSON object rather
    than a double-encoded string.
    """
    try:
        payload: JsonDict = json.loads(entry.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {}
    event_type = entry.get("type", "")
    data = {
        "id": entry.get("id", ""),
        "execution_id": entry.get("execution_id", ""),
        "sequence": int(entry["sequence"]) if entry.get("sequence") else None,
        "type": event_type,
        "severity": entry.get("severity", ""),
        "node_id": entry.get("node_id") or None,
        "agent_id": entry.get("agent_id") or None,
        "tool": entry.get("tool") or None,
        "message": entry.get("message", ""),
        "payload": payload,
        "created_at": entry.get("created_at", ""),
    }
    stream_id = entry.get("_id", "")
    return f"id: {stream_id}\nevent: {event_type or 'message'}\ndata: {json.dumps(data)}\n\n"


async def _stream_events(
    execution_id: str, app_state: AppState, after_id: str
) -> AsyncIterator[str]:
    """Backlog, then live: replay from ``after_id``, then follow the stream.

    Stops as soon as a terminal execution event is seen or the execution is
    already terminal -- there is nothing more to wait for, so the connection
    closes instead of blocking on ``XREAD`` forever.
    """
    cursor = after_id
    backlog = await app_state.redis.read_events(execution_id, after=after_id, count=10_000)
    for entry in backlog:
        cursor = entry["_id"]
        yield _format_sse(entry)
        if entry.get("type") in _TERMINAL_STREAM_EVENT_VALUES:
            return

    state = await _load_state(execution_id, app_state)
    if state.status.is_terminal:
        return

    async for entry in app_state.redis.tail_events(execution_id, last_id=cursor):
        yield _format_sse(entry)
        if entry.get("type") in _TERMINAL_STREAM_EVENT_VALUES:
            return


@router.get("/{execution_id}/stream")
async def stream_execution_events(
    execution_id: str,
    after_id: str = "-",
    app_state: AppState = Depends(get_app_state),
) -> StreamingResponse:
    """Live execution events over Server-Sent Events.

    Best-effort, not authoritative: events are read from the Redis stream
    :class:`~orchestration.coordination.redis.RedisEventSink` publishes to,
    which is capped (10,000 entries) and -- per the event bus's own
    fault-tolerance contract, where a sink failure is recorded but never
    fails the execution -- can silently miss an event if Redis was briefly
    unavailable when it was emitted. ``GET /executions/{id}/events`` against
    PostgreSQL remains the durable, complete history; this endpoint is for a
    live view, not an audit trail.

    ``after_id`` resumes a dropped connection from a specific Redis stream id
    (the ``id`` field of the last SSE message received); left at its default
    ``"-"``, the full backlog held in the stream replays before live events
    start.

    Requires the same ``X-API-Key`` header as every other route on this
    router -- a plain browser ``EventSource`` cannot set a custom header, so
    a browser client needs a ``fetch`` + ``ReadableStream`` reader (or a
    server-side proxy) rather than ``EventSource`` directly.
    """
    await _load_state(execution_id, app_state)  # raises NotFoundError if unknown
    return StreamingResponse(
        _stream_events(execution_id, app_state, after_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{execution_id}/trace")
async def get_execution_trace(
    execution_id: str, app_state: AppState = Depends(get_app_state)
) -> JsonDict:
    """An event-derived trace view.

    Not a live span query against the OTel backend -- this deployment exports
    spans over OTLP for a real tracing UI to render (see
    :mod:`orchestration.observability.tracing`), and nothing here re-implements
    that. What is genuinely available without one is reconstructed instead:
    the ordered event log, each entry carrying the ``trace_id``/``span_id`` it
    was recorded under.
    """
    async with app_state.database.session() as session:
        events = await EventRepository(session).query(
            execution_id, EventFilter(limit=10_000)
        )
    trace_ids = sorted({e.trace_id for e in events if e.trace_id})
    return {
        "execution_id": execution_id,
        "trace_ids": trace_ids,
        "events": [
            {
                "sequence": e.sequence,
                "type": e.type.value,
                "node_id": e.node_id,
                "trace_id": e.trace_id,
                "span_id": e.span_id,
                "message": e.message,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
    }
