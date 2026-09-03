"""``/observability`` -- durable, database-backed operational summary.

Distinct from ``GET /metrics``: that endpoint exposes live Prometheus
counters for a real scraper (Grafana/Prometheus), and resets to zero on
every process restart. This one reads what already durably persists --
executions, agent/tool invocations, events -- so the numbers survive a
restart and reflect this deployment's actual history, not just its current
process's uptime.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestration.api.schemas import ObservabilitySummary
from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.persistence.repositories import ObservabilityRepository

router = APIRouter(
    prefix="/observability", tags=["observability"], dependencies=[Depends(require_api_key)]
)


@router.get("", response_model=ObservabilitySummary)
async def get_observability_summary(
    app_state: AppState = Depends(get_app_state),
) -> ObservabilitySummary:
    async with app_state.database.session() as session:
        repo = ObservabilityRepository(session)
        totals = await repo.execution_totals()
        agents = await repo.agent_stats()
        tools = await repo.tool_stats()
        issues = await repo.recent_issues()
    return ObservabilitySummary(
        totals=totals,  # type: ignore[arg-type]
        agents=tuple(agents),  # type: ignore[arg-type]
        tools=tuple(tools),  # type: ignore[arg-type]
        recent_issues=tuple(issues),  # type: ignore[arg-type]
    )
