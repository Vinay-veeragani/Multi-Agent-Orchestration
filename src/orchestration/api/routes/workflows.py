"""``/workflows`` -- registering and inspecting hand-authored workflows."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.domain.workflow import Workflow
from orchestration.errors import GraphValidationError
from orchestration.persistence.repositories import WorkflowRepository
from orchestration.workflow.graph import WorkflowGraph

router = APIRouter(prefix="/workflows", tags=["workflows"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=Workflow, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    workflow: Workflow, app_state: AppState = Depends(get_app_state)
) -> Workflow:
    """Register a workflow, rejecting one that could never execute.

    Validated against this process's live agent and tool registries before it
    ever reaches the database -- the same :class:`WorkflowGraph.validate` the
    executor itself trusts, so a workflow accepted here is one the engine can
    actually run, not merely one that parses.
    """
    problems = WorkflowGraph(workflow).validate(
        known_agents=app_state.agents.ids(include_disabled=True),
        known_tools=app_state.tools.names(include_disabled=True),
    )
    if problems:
        raise GraphValidationError(
            f"workflow {workflow.id!r} is not executable", problems=problems
        )
    async with app_state.database.session() as session:
        await WorkflowRepository(session).save(workflow)
    return workflow


@router.get("", response_model=list[Workflow])
async def list_workflows(
    limit: int = 100, app_state: AppState = Depends(get_app_state)
) -> list[Workflow]:
    async with app_state.database.session() as session:
        return await WorkflowRepository(session).list_all(limit=limit)


@router.get("/{workflow_id}", response_model=Workflow)
async def get_workflow(workflow_id: str, app_state: AppState = Depends(get_app_state)) -> Workflow:
    async with app_state.database.session() as session:
        return await WorkflowRepository(session).get(workflow_id)
