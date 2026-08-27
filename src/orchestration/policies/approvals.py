"""Approval service: the durable half of human-in-the-loop.

The problem this solves is not "how do we pause" -- the executor already returns
when a gate is reached. It is what happens on the way back.

A resumed execution re-runs the node that paused it. If that node simply raised
:class:`ApprovalRequired` again, the execution would pause forever. So an
approval needs an identity that is **derived from what is being approved**, not
generated per attempt:

    key = sha256(execution_id, node_id, action, redacted arguments)

The same gate reached twice produces the same key, so the second attempt finds
the existing record and reads its decision. That is what makes the pause
survivable across a process restart, and it is also what makes ``POST /approve``
idempotent: a double-clicked button decides the same record twice.

Approved arguments are matched too. If an agent asks to email ``a@b.test`` and a
reviewer approves it, the agent cannot then email ``c@d.test`` under that
approval -- the arguments are part of the key, so different arguments are a
different request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from orchestration.domain.approval import ApprovalRequest
from orchestration.domain.base import JsonDict
from orchestration.domain.enums import ApprovalStatus, EventType, RiskLevel
from orchestration.errors import ApprovalRejectedError, NotFoundError
from orchestration.events.bus import EventBus
from orchestration.observability.metrics import record_approval
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import ApprovalRepository


def approval_key(
    *,
    execution_id: str,
    action: str,
    node_id: str | None = None,
    arguments: JsonDict | None = None,
) -> str:
    """Deterministic identity for one approvable action.

    Includes the arguments so an approval cannot be reused for a different call:
    approving an email to one recipient must not authorise an email to another.
    """
    payload = json.dumps(
        {
            "execution_id": execution_id,
            "node_id": node_id,
            "action": action,
            "arguments": arguments or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


@dataclass(slots=True)
class ApprovalOutcome:
    """What the gate should do, given the current state of an approval."""

    request: ApprovalRequest
    #: True when the action may proceed now.
    granted: bool
    #: True when a human refused; the node must fail rather than retry.
    rejected: bool
    #: True when nothing has been decided yet and the execution must pause.
    pending: bool
    #: Arguments to actually use -- a reviewer may have edited them.
    effective_arguments: JsonDict


class ApprovalService:
    """Creates, resolves, and decides approval requests.

    Args:
        database: Durable store. Approvals must outlive the process, so there is
            no in-memory mode.
        events: Optional bus for approval lifecycle events.
        default_ttl_seconds: Expiry applied to new requests. ``None`` means an
            approval waits indefinitely, which is occasionally what an operator
            wants but is a poor default -- an execution parked forever holds a
            slot and never reports.
    """

    def __init__(
        self,
        database: Database,
        *,
        events: EventBus | None = None,
        default_ttl_seconds: float | None = 3_600.0,
    ) -> None:
        self._db = database
        self._events = events
        self._ttl = default_ttl_seconds

    # -- the gate ----------------------------------------------------------

    async def resolve(
        self,
        *,
        execution_id: str,
        action: str,
        risk_reason: str,
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        arguments: JsonDict | None = None,
        risk_level: RiskLevel = RiskLevel.HIGH,
    ) -> ApprovalOutcome:
        """Find or create the approval for this action and report its state.

        This is the single call a gate makes. On a first encounter it creates a
        pending request; on a resumed encounter it finds the existing one and
        returns whatever a human decided.
        """
        identity = approval_key(
            execution_id=execution_id, action=action, node_id=node_id, arguments=arguments
        )
        request_id = f"appr_{identity}"

        async with self._db.session() as session:
            repo = ApprovalRepository(session)
            existing: ApprovalRequest | None = None
            try:
                existing = await repo.get(request_id)
            except NotFoundError:
                existing = None

            if existing is None:
                created = ApprovalRequest(
                    id=request_id,
                    execution_id=execution_id,
                    node_id=node_id,
                    action=action,
                    agent_id=agent_id,
                    tool=tool,
                    parameters=arguments or {},
                    risk_level=risk_level,
                    risk_reason=risk_reason,
                )
                if self._ttl is not None:
                    from datetime import timedelta

                    created = created.merged(
                        expires_at=created.requested_at + timedelta(seconds=self._ttl)
                    )
                existing = await repo.create(created)

                if self._events is not None:
                    await self._events.emit(
                        EventType.APPROVAL_REQUESTED,
                        execution_id=execution_id,
                        message=risk_reason,
                        node_id=node_id,
                        agent_id=agent_id,
                        tool=tool,
                        payload={
                            "approval_id": existing.id,
                            "action": action,
                            "risk_level": risk_level.value,
                        },
                    )

        # Expiry is evaluated on read rather than by a background sweeper: the
        # only moment it matters is when a gate is deciding what to do, and a
        # sweeper would add a moving part for no behavioural gain.
        if existing.is_pending and existing.is_expired_at():
            existing = await self._mark_expired(existing)

        return ApprovalOutcome(
            request=existing,
            granted=existing.status is ApprovalStatus.APPROVED,
            rejected=existing.status
            in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED},
            pending=existing.is_pending,
            effective_arguments=existing.effective_parameters,
        )

    async def require(
        self,
        *,
        execution_id: str,
        action: str,
        risk_reason: str,
        node_id: str | None = None,
        agent_id: str | None = None,
        tool: str | None = None,
        arguments: JsonDict | None = None,
        risk_level: RiskLevel = RiskLevel.HIGH,
    ) -> JsonDict:
        """Return the arguments to use, or raise to pause / refuse.

        Raises:
            ApprovalRequired: Nothing decided yet -- the execution must pause.
            ApprovalRejectedError: A human refused, or the request expired.
                Terminal by classification, so no retry loop re-asks a reviewer
                who already said no.
        """
        from orchestration.errors import ApprovalRequired

        outcome = await self.resolve(
            execution_id=execution_id,
            action=action,
            risk_reason=risk_reason,
            node_id=node_id,
            agent_id=agent_id,
            tool=tool,
            arguments=arguments,
            risk_level=risk_level,
        )

        if outcome.granted:
            return outcome.effective_arguments
        if outcome.rejected:
            raise ApprovalRejectedError(
                f"{action} was not approved ({outcome.request.status.value})",
                approval_id=outcome.request.id,
                status=outcome.request.status.value,
                note=outcome.request.decision_note,
            )
        raise ApprovalRequired(
            outcome.request.risk_reason,
            approval_id=outcome.request.id,
            action=action,
            node_id=node_id,
            agent=agent_id,
            tool=tool,
            arguments=outcome.request.parameters,
        )

    # -- decisions ---------------------------------------------------------

    async def approve(
        self,
        approval_id: str,
        *,
        by: str,
        note: str | None = None,
        modified_arguments: JsonDict | None = None,
    ) -> ApprovalRequest:
        """Record an approval.

        ``modified_arguments`` lets a reviewer narrow a request rather than
        having to reject it outright -- approving an email but changing the
        recipient list, say.
        """
        async with self._db.transaction() as session:
            decided = await ApprovalRepository(session).decide(
                approval_id,
                status=ApprovalStatus.APPROVED,
                decided_by=by,
                note=note,
                modified=modified_arguments,
            )
        if self._events is not None:
            await self._events.emit(
                EventType.APPROVAL_GRANTED,
                execution_id=decided.execution_id,
                message=f"{decided.action} approved by {by}",
                node_id=decided.node_id,
                tool=decided.tool,
                payload={"approval_id": decided.id, "modified": bool(modified_arguments)},
            )
        record_approval(decided.status.value)
        return decided

    async def reject(
        self, approval_id: str, *, by: str, note: str | None = None
    ) -> ApprovalRequest:
        async with self._db.transaction() as session:
            decided = await ApprovalRepository(session).decide(
                approval_id, status=ApprovalStatus.REJECTED, decided_by=by, note=note
            )
        if self._events is not None:
            await self._events.emit(
                EventType.APPROVAL_REJECTED,
                execution_id=decided.execution_id,
                message=f"{decided.action} rejected by {by}",
                node_id=decided.node_id,
                tool=decided.tool,
                payload={"approval_id": decided.id, "note": note},
            )
        record_approval(decided.status.value)
        return decided

    # -- queries -----------------------------------------------------------

    async def get(self, approval_id: str) -> ApprovalRequest:
        async with self._db.session() as session:
            return await ApprovalRepository(session).get(approval_id)

    async def pending_for(self, execution_id: str) -> list[ApprovalRequest]:
        async with self._db.session() as session:
            return await ApprovalRepository(session).pending_for(execution_id)

    async def expire_overdue(self) -> int:
        """Mark every overdue pending approval expired.

        Exposed for an operator or a scheduled job. Not required for
        correctness -- :meth:`resolve` evaluates expiry on read -- but useful for
        keeping a dashboard truthful.
        """
        async with self._db.transaction() as session:
            return await ApprovalRepository(session).expire_overdue()

    async def _mark_expired(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._db.transaction() as session:
            await ApprovalRepository(session).decide(
                request.id,
                status=ApprovalStatus.EXPIRED,
                decided_by="system",
                note="expired before a decision was made",
            )
        if self._events is not None:
            await self._events.emit(
                EventType.APPROVAL_EXPIRED,
                execution_id=request.execution_id,
                message=f"{request.action} expired without a decision",
                node_id=request.node_id,
                payload={"approval_id": request.id},
            )
        record_approval(ApprovalStatus.EXPIRED.value)
        return await self.get(request.id)

    # -- executor integration ---------------------------------------------

    def gate(self) -> object:
        """Adapter matching the executor's ``ApprovalGate`` signature.

        Returns ``(status, approval_id, note)``. The executor dispatches on the
        status string rather than on an exception, because "granted" is a normal
        outcome that lets the node succeed -- not an error to be caught.
        """

        async def _gate(execution_id: str, node_id: str, risk_reason: str) -> tuple[str, str, str]:
            outcome = await self.node_gate(
                execution_id=execution_id,
                node_id=node_id,
                action=f"node:{node_id}",
                risk_reason=risk_reason,
            )
            if outcome.granted:
                status = "granted"
            elif outcome.rejected:
                status = "rejected"
            else:
                status = "pending"
            note = outcome.request.decision_note or outcome.request.status.value
            return status, outcome.request.id, note

        return _gate

    def approval_creator(self) -> object:
        """Adapter matching the executor's ``ApprovalCreator`` signature.

        The executor's approval *node* only needs a request created and its id
        returned; deciding whether to proceed is handled by
        :meth:`node_gate`, which the executor consults first.
        """

        async def _create(
            execution_id: str,
            node_id: str,
            action: str,
            parameters: JsonDict,
            risk_reason: str,
        ) -> str:
            outcome = await self.resolve(
                execution_id=execution_id,
                action=action,
                risk_reason=risk_reason,
                node_id=node_id,
                arguments=parameters,
            )
            return outcome.request.id

        return _create

    async def node_gate(
        self, *, execution_id: str, node_id: str, action: str, risk_reason: str
    ) -> ApprovalOutcome:
        """Resolve the approval for a workflow approval node."""
        return await self.resolve(
            execution_id=execution_id,
            action=action,
            risk_reason=risk_reason,
            node_id=node_id,
            arguments={"node": node_id},
        )

    def tool_authoriser(self, inner: object, *, execution_id: str) -> object:
        """Wrap a policy authoriser so granted approvals let a call through.

        Without this the agent runtime would re-request approval for a tool a
        human already authorised, and the execution would pause in a loop. The
        wrapper sits between the runtime and the policy engine: the policy still
        decides *whether* approval is needed, and this decides whether it has
        already been given.
        """
        from orchestration.domain.enums import PolicyEffect

        async def _authorise(
            agent_id: str, tool: str, arguments: JsonDict
        ) -> tuple[PolicyEffect, str]:
            effect, reason = await inner(agent_id, tool, arguments)  # type: ignore[operator]
            if effect is not PolicyEffect.REQUIRE_APPROVAL:
                return effect, reason

            outcome = await self.resolve(
                execution_id=execution_id,
                action=f"tool:{tool}",
                risk_reason=reason,
                agent_id=agent_id,
                tool=tool,
                arguments=arguments,
            )
            if outcome.granted:
                return PolicyEffect.ALLOW, (
                    f"approved by {outcome.request.decided_by or 'a reviewer'}"
                )
            if outcome.rejected:
                return PolicyEffect.DENY, (
                    f"not approved ({outcome.request.status.value})"
                    + (
                        f": {outcome.request.decision_note}"
                        if outcome.request.decision_note
                        else ""
                    )
                )
            return PolicyEffect.REQUIRE_APPROVAL, reason

        return _authorise


async def reject_and_fail(service: ApprovalService, approval_id: str, *, by: str) -> None:
    """Reject an approval and raise, for a caller that wants the error form."""
    decided = await service.reject(approval_id, by=by)
    raise ApprovalRejectedError(
        f"{decided.action} was rejected by {by}",
        approval_id=approval_id,
    )
