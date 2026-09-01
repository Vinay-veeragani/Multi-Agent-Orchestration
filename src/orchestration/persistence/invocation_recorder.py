"""Bridges the engine's writer-callable pattern to `InvocationRepository`.

`WorkflowExecutor`'s `invocation_recorder` and `AgentRuntime`'s
`tool_observer` are optional, best-effort hooks (mirroring `CheckpointWriter`
-- an execution's correctness never depends on either succeeding). This
module is the one place that turns them into real writes, so
`agent_invocations`/`tool_invocations` are actually populated for a live
execution instead of only being exercised by persistence-layer unit tests.
"""

from __future__ import annotations

from orchestration.domain.agent import AgentInvocation
from orchestration.domain.base import JsonDict, new_id
from orchestration.domain.enums import InvocationStatus
from orchestration.domain.tool import ToolInvocation, ToolResult
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import InvocationRepository


class InvocationRecorder:
    """Writes agent/tool invocation audit rows outside any other transaction.

    Each write is its own committed session: invocation records are audit
    trail the rest of the engine does not depend on being atomic with a
    checkpoint, so a slow or failing write here must never hold up (or roll
    back) the state transition that produced it.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def record_agent(self, invocation: AgentInvocation) -> None:
        async with self._db.session() as session:
            await InvocationRepository(session).record_agent(invocation)

    async def record_tool(
        self,
        execution_id: str,
        node_id: str | None,
        agent_id: str,
        arguments: JsonDict,
        result: ToolResult,
    ) -> None:
        """Record one completed tool call.

        Writes via ``claim_tool`` then ``complete_tool`` -- the pair
        `InvocationRepository` was built for -- but only as a convenient way
        to write one finished row after the fact, with a fresh id standing
        in for ``idempotency_key``. This is *not* the pre-call idempotency
        reservation those methods' own docstrings describe: nothing in the
        engine claims a key before a tool actually runs (see
        docs/PHASE2_ARCHITECTURE_AUDIT.md), so no resume-skip behaviour
        depends on, or is added by, this call -- it is audit trail only.
        """
        invocation = ToolInvocation(
            execution_id=execution_id,
            node_id=node_id,
            agent_id=agent_id,
            tool=result.tool,
            arguments=arguments,
            attempt=result.attempts,
            status=InvocationStatus.SUCCEEDED if result.ok else InvocationStatus.FAILED,
            result=result.output if result.ok else None,
            error=(
                None
                if result.ok
                else {"code": result.error_code, "message": result.error_message}
            ),
            idempotency_key=new_id("tinv"),
        )
        async with self._db.session() as session:
            repo = InvocationRepository(session)
            if await repo.claim_tool(invocation):
                await repo.complete_tool(
                    invocation.idempotency_key,
                    status=invocation.status.value,
                    result=invocation.result,
                    error=invocation.error,
                    duration_seconds=result.duration_seconds,
                )
