"""``/health`` and ``/metrics`` -- unauthenticated operational endpoints.

Exempt from the API key requirement deliberately: a load balancer health probe
and a Prometheus scraper are not "clients of the engine" in the sense the rest
of the API guards against, and gating them behind a secret only complicates
routine infrastructure wiring for no real security benefit in this
deployment's threat model.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from orchestration.api.schemas import HealthResponse
from orchestration.api.security import get_app_state
from orchestration.api.state import AppState
from orchestration.observability.metrics import metrics_endpoint

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(app_state: AppState = Depends(get_app_state)) -> HealthResponse:
    database_ok = await app_state.database.ping()
    redis_ok = await app_state.redis.ping()
    return HealthResponse(
        status="ok" if (database_ok and redis_ok) else "degraded",
        database=database_ok,
        redis=redis_ok,
    )


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = metrics_endpoint()
    return Response(content=body, media_type=content_type)
