"""``/benchmarks`` -- reading evaluation reports the CLI/harness already writes.

``orchestrator benchmark`` (see docs/evaluation-benchmark.md) persists a full
:class:`~orchestration.domain.evaluation.BenchmarkReport` via
:meth:`~orchestration.persistence.repositories.BenchmarkRepository.save` on
every run; nothing here changes how or when that happens. These are the
first routes to read that data back over HTTP, for an evaluation dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.domain.base import JsonDict
from orchestration.domain.evaluation import BenchmarkReport
from orchestration.persistence.repositories import BenchmarkRepository

router = APIRouter(
    prefix="/benchmarks", tags=["benchmarks"], dependencies=[Depends(require_api_key)]
)


@router.get("")
async def list_benchmark_runs(
    limit: int = 20, app_state: AppState = Depends(get_app_state)
) -> list[JsonDict]:
    async with app_state.database.session() as session:
        return await BenchmarkRepository(session).list_recent(limit=limit)


@router.get("/{report_id}", response_model=BenchmarkReport)
async def get_benchmark_run(
    report_id: str, app_state: AppState = Depends(get_app_state)
) -> BenchmarkReport:
    async with app_state.database.session() as session:
        return await BenchmarkRepository(session).get(report_id)
