"""``/agents`` -- registering and inspecting agent definitions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.domain.agent import AgentDefinition
from orchestration.persistence.repositories import AgentRepository

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=AgentDefinition, status_code=status.HTTP_201_CREATED)
async def create_agent(
    definition: AgentDefinition, app_state: AppState = Depends(get_app_state)
) -> AgentDefinition:
    """Register (or update) an agent definition.

    Persisted first -- so it survives a restart and is visible to every
    process -- then added to this process's in-memory registry, which is what
    the supervisor and executor actually consult while routing.
    """
    async with app_state.database.session() as session:
        await AgentRepository(session).upsert(definition)
    app_state.agents.register(definition, replace=True)
    return definition


@router.get("", response_model=list[AgentDefinition])
async def list_agents(
    include_disabled: bool = False, app_state: AppState = Depends(get_app_state)
) -> list[AgentDefinition]:
    async with app_state.database.session() as session:
        return await AgentRepository(session).list_all(include_disabled=include_disabled)


@router.get("/{agent_id}", response_model=AgentDefinition)
async def get_agent(agent_id: str, app_state: AppState = Depends(get_app_state)) -> AgentDefinition:
    async with app_state.database.session() as session:
        return await AgentRepository(session).get(agent_id)
