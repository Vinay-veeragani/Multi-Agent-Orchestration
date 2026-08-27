"""The FastAPI application factory.

A factory rather than a module-level ``app`` object: tests build a fresh app
(and a fresh :class:`~orchestration.api.state.AppState`) per case, which is
what lets integration tests exercise the real ASGI app over an in-process
transport without any process actually listening on a port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestration.api.errors import install_exception_handlers
from orchestration.api.routes import api_router
from orchestration.api.state import AppState, build_app_state, close_app_state
from orchestration.config import Settings, get_settings
from orchestration.coordination.redis import RedisCoordinator
from orchestration.llm.factory import LLMClient
from orchestration.observability.logging import configure_logging
from orchestration.observability.tracing import configure_tracing
from orchestration.persistence.database import Database


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    database: Database | None = None,
    redis: RedisCoordinator | None = None,
) -> FastAPI:
    """Build the ASGI app.

    ``llm``/``database``/``redis`` pass straight through to
    :func:`~orchestration.api.state.build_app_state` -- see there for why a
    test would supply them.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)
    configure_tracing(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.app_state = await build_app_state(
            resolved, llm=llm, database=database, redis=redis
        )
        try:
            yield
        finally:
            await close_app_state(app.state.app_state)

    app = FastAPI(
        title="agent-orchestration-engine",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_exception_handlers(app)
    app.include_router(api_router)
    return app


__all__ = ["AppState", "create_app"]
