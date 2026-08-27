"""``/executions`` -- creating, inspecting, and steering executions.

Creation returns as soon as the run is durably queued; the run itself happens
in the background via :class:`~orchestration.api.runner.ExecutionRunner`. Every
other route here is a thin read or a signal into that runner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

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
from orchestration.domain.events import EventFilter, ExecutionEvent
from orchestration.domain.execution import ExecutionState
from orchestration.domain.workflow import Task
from orchestration.errors import InputValidationError, NotFoundError
from orchestration.persistence.repositories import (
    EventRepository,
    ExecutionRepository,
    WorkflowRepository,
)
from orchestration.policies.approvals import ApprovalService
from orchestration.runtime.orchestrator import seed_dynamic_workflow

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
