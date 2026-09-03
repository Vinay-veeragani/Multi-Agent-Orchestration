"""``/approvals`` -- the HITL inbox: every pending approval, across every execution.

Distinct from ``/executions/{id}/approvals``, which scopes to one run a
caller already has open. This is the "what needs a human right now, across
the whole deployment" view -- deciding one still goes through the same
``POST /executions/{id}/approve``/``/reject`` those per-execution routes use;
this router only reads.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestration.api.schemas import PendingApprovalItem
from orchestration.api.security import get_app_state, require_api_key
from orchestration.api.state import AppState
from orchestration.persistence.repositories import ApprovalRepository

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[PendingApprovalItem])
async def list_pending_approvals(
    limit: int = 100, app_state: AppState = Depends(get_app_state)
) -> list[PendingApprovalItem]:
    async with app_state.database.session() as session:
        pairs = await ApprovalRepository(session).list_all_pending(limit=limit)
    return [
        PendingApprovalItem(
            id=approval.id,
            execution_id=approval.execution_id,
            task_description=task_description,
            node_id=approval.node_id,
            action=approval.action,
            agent_id=approval.agent_id,
            tool=approval.tool,
            parameters=approval.parameters,
            risk_level=approval.risk_level,
            risk_reason=approval.risk_reason,
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
        )
        for approval, task_description in pairs
    ]
