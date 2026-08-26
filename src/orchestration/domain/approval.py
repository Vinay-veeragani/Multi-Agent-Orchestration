"""Human-in-the-loop approval requests.

An approval request is a durable record, not an in-memory future. The execution
that raised it is checkpointed and released; the decision arrives later over the
API, possibly after a process restart, and resumption reads the decision from the
database. Nothing about the pause depends on the original coroutine surviving.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from orchestration.domain.base import (
    BoundedText,
    DomainModel,
    JsonDict,
    Slug,
    id_factory,
    utc_now,
)
from orchestration.domain.enums import ApprovalStatus, RiskLevel


class ApprovalRequest(DomainModel):
    """A pending or decided request for human authorisation.

    The payload is deliberately complete enough to review without consulting
    another system: what action, by which agent, with exactly which arguments,
    and why it was escalated.
    """

    id: str = Field(default_factory=id_factory("approval"))
    execution_id: str
    node_id: Slug | None = None
    #: The action awaiting authorisation, e.g. ``"tool:send_email"``.
    action: str = Field(min_length=1, max_length=256)
    agent_id: Slug | None = None
    tool: Slug | None = None
    #: Exact arguments that will be used if approved. Sensitive values are
    #: masked before this is stored, so the audit record cannot leak a secret.
    parameters: JsonDict = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.HIGH
    #: Why a human is being asked -- shown verbatim to the reviewer.
    risk_reason: BoundedText = Field(min_length=1)
    status: ApprovalStatus = ApprovalStatus.PENDING

    requested_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: str | None = Field(default=None, max_length=128)
    decision_note: BoundedText | None = None
    #: Arguments substituted by the reviewer, when a modified approval is allowed.
    modified_parameters: JsonDict | None = None

    @classmethod
    def create(
        cls,
        *,
        execution_id: str,
        action: str,
        risk_reason: str,
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        parameters: JsonDict | None = None,
        risk_level: RiskLevel = RiskLevel.HIGH,
        ttl_seconds: float | None = None,
    ) -> ApprovalRequest:
        """Build a pending request, optionally with an expiry."""
        now = utc_now()
        return cls(
            execution_id=execution_id,
            node_id=node_id,
            action=action,
            agent_id=agent_id,
            tool=tool,
            parameters=parameters or {},
            risk_level=risk_level,
            risk_reason=risk_reason,
            requested_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        )

    # -- decisions ---------------------------------------------------------

    def approve(
        self, *, by: str, note: str | None = None, modified: JsonDict | None = None
    ) -> None:
        """Record an approval.

        Idempotent for a repeated identical approval, so a double-clicked button
        or a retried HTTP request does not error. A *conflicting* decision still
        raises, because silently overwriting a rejection would be dangerous.
        """
        self._guard_decision(ApprovalStatus.APPROVED)
        if self.status is ApprovalStatus.APPROVED:
            return
        self.status = ApprovalStatus.APPROVED
        self.decided_at = utc_now()
        self.decided_by = by
        self.decision_note = note
        self.modified_parameters = modified

    def reject(self, *, by: str, note: str | None = None) -> None:
        """Record a rejection."""
        self._guard_decision(ApprovalStatus.REJECTED)
        if self.status is ApprovalStatus.REJECTED:
            return
        self.status = ApprovalStatus.REJECTED
        self.decided_at = utc_now()
        self.decided_by = by
        self.decision_note = note

    def expire(self) -> None:
        if self.status is ApprovalStatus.PENDING:
            self.status = ApprovalStatus.EXPIRED
            self.decided_at = utc_now()

    def cancel(self) -> None:
        if self.status is ApprovalStatus.PENDING:
            self.status = ApprovalStatus.CANCELLED
            self.decided_at = utc_now()

    def _guard_decision(self, target: ApprovalStatus) -> None:
        from orchestration.errors import InvalidStateTransitionError

        if self.status.is_decided and self.status is not target:
            raise InvalidStateTransitionError(
                f"approval {self.id} is already {self.status.value} "
                f"and cannot become {target.value}",
                approval_id=self.id,
                current=self.status.value,
                requested=target.value,
            )

    # -- queries -----------------------------------------------------------

    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED

    def is_expired_at(self, when: datetime | None = None) -> bool:
        """Whether the request has passed its expiry."""
        if self.expires_at is None:
            return False
        return (when or utc_now()) >= self.expires_at

    @property
    def effective_parameters(self) -> JsonDict:
        """Arguments to actually use -- reviewer edits win over the original."""
        return self.modified_parameters if self.modified_parameters is not None else self.parameters

    def to_review_payload(self) -> JsonDict:
        """What the API returns to a reviewer."""
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "action": self.action,
            "agent": self.agent_id,
            "tool": self.tool,
            "parameters": self.parameters,
            "risk_level": self.risk_level.value,
            "risk_reason": self.risk_reason,
            "status": self.status.value,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
