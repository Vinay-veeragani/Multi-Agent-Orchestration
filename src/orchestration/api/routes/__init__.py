"""All API routers, aggregated for :func:`orchestration.api.app.create_app`."""

from __future__ import annotations

from fastapi import APIRouter

from orchestration.api.routes import agents, executions, system, workflows

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(agents.router)
api_router.include_router(workflows.router)
api_router.include_router(executions.router)

__all__ = ["api_router"]
