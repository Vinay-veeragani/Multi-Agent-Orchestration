#!/usr/bin/env python
"""Demo: human-in-the-loop approval, across a simulated process restart.

Task: draft a customer status update, then pause for a human decision before
treating the email as ready to send. Shows the durable half of
human-in-the-loop: the run pauses, the process that paused it is discarded
entirely, a *different* orchestrator (fresh Supervisor, AgentRuntime, event
bus -- everything) is built from nothing but what is in the database, and it
resumes correctly whether a reviewer approved or rejected.

Run from the repository root::

    python examples/human_approval/run.py                 # approves
    python examples/human_approval/run.py --decision reject
    python examples/human_approval/run.py --test-db
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (
    build_engine,
    common_arg_parser,
    print_banner,
    print_final,
    resume_dynamic_run,
    run,
    start_dynamic_run,
)

from orchestration.domain.enums import ExecutionStatus
from orchestration.llm.mock import (
    MockProvider,
    MockRule,
    agent_output,
    routing_decision,
)
from orchestration.policies.approvals import ApprovalService

TASK = "Draft a status update email for Acme Corp, then prepare it to send."

_DRAFT = (
    "Subject: Your Q3 Account Status\n\n"
    "Hi Acme team,\n\n"
    "Your account is in good standing. All three integrations are live and "
    "usage is trending 12% above last quarter.\n\n"
    "Best,\nSupport Team"
)


def _provider(decision: str) -> MockProvider:
    """Script the supervisor's third decision to match what actually happens.

    The orchestrator does not re-ask "was this approved?" as a fresh
    ``request_human_approval`` decision on resume -- it simply asks the
    supervisor what to do next, having seen the decision already recorded.
    A real model would see that in its context and finalise or fail
    accordingly; the mock has to be told which one to say.
    """
    third_response = (
        routing_decision("finalize", answer="Email approved and marked ready to send.")
        if decision == "approve"
        else routing_decision("fail", failure_reason="the send request was rejected by a reviewer")
    )
    return MockProvider(
        [
            MockRule(
                name="supervisor",
                match_system="supervisor",
                priority=10,
                responses=(
                    routing_decision(
                        "delegate",
                        agents=["finalizer_agent"],
                        instructions=["Draft a status update email for Acme Corp."],
                        reason="draft the email before anything external happens",
                    ),
                    routing_decision(
                        "request_human_approval",
                        approval_action="send the drafted status email to Acme Corp",
                        approval_risk_reason=(
                            "external customer communication -- irreversible once sent"
                        ),
                    ),
                    third_response,
                ),
            ),
            MockRule(
                name="finalizer",
                match_request_key=":finalizer_agent:",
                responses=(agent_output(_DRAFT, confidence=0.9),),
            ),
        ]
    )


async def main() -> None:
    parser = common_arg_parser(__doc__ or "")
    parser.add_argument(
        "--decision",
        choices=("approve", "reject"),
        default="approve",
        help="What the simulated reviewer decides (default: approve).",
    )
    args = parser.parse_args()
    print_banner("Demo: Human Approval")

    engine = await build_engine(
        test_db=args.test_db,
        provider=_provider(args.decision),
        sandbox_root=Path(__file__).parent / "workspace",
    )
    try:
        print(f"task: {TASK}\n")
        print("--- process 1: runs until it needs a human decision ---")
        demo = await start_dynamic_run(engine, TASK)
        execution_id = demo.state.execution_id
        paused = await demo.orchestrator.run(demo.state, demo.workflow)

        if not paused.is_paused:
            print("(did not pause -- nothing to approve)")
            print_final(paused.state)
            return

        approval_id = paused.state.pending_approval_id
        assert approval_id is not None
        pending = await demo.approvals.get(approval_id)
        print(f"\n>>> PAUSED: {pending.action!r}")
        print(f">>> risk: {pending.risk_reason}")
        print(f">>> approval_id: {approval_id}\n")

        print(f"--- a reviewer decides (out of band): {args.decision} ---")
        reviewer = ApprovalService(engine.database)
        if args.decision == "approve":
            await reviewer.approve(
                approval_id, by="demo-operator@example.test", note="looks correct"
            )
        else:
            await reviewer.reject(
                approval_id, by="demo-operator@example.test", note="not ready yet"
            )

        print("\n--- process 2: a fresh orchestrator, built from nothing but the database ---")
        resumed = await resume_dynamic_run(engine, execution_id)
        result = await resumed.orchestrator.run(resumed.state, resumed.workflow)

        print_final(result.state)
        if args.decision == "reject":
            assert result.state.status is ExecutionStatus.FAILED
        else:
            assert result.state.status is ExecutionStatus.SUCCEEDED
    finally:
        await engine.aclose()


if __name__ == "__main__":
    run(main())
