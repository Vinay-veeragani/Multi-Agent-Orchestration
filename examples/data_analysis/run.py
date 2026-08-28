#!/usr/bin/env python
"""Demo: data analysis with a real tool call.

Task: compute total and average quarterly revenue from a small dataset, then
summarise the trend. Shows an agent actually calling a tool (``python_exec``
for data_agent, ``calculator`` for analyst_agent) mid-run -- the tool result is
fed back into the agent's own reasoning before it answers, and the call only
succeeds because the agent's permission allowlist includes that tool.

Run from the repository root::

    python examples/data_analysis/run.py
    python examples/data_analysis/run.py --test-db
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (
    build_engine,
    common_arg_parser,
    print_banner,
    print_final,
    run,
    start_dynamic_run,
)

from orchestration.llm.mock import (
    MockProvider,
    MockRule,
    agent_output,
    routing_decision,
)

TASK = (
    "Given quarterly revenue figures of 120000, 95000, 143000, and 158000, "
    "compute the total and average, then summarise the trend."
)

_TOTAL = 120_000 + 95_000 + 143_000 + 158_000
_AVERAGE = round(_TOTAL / 4, 2)
_FINAL_ANSWER = (
    f"Total revenue across the four quarters: ${_TOTAL:,}. "
    f"Average per quarter: ${_AVERAGE:,.2f}. "
    "Trend: a dip in Q2 (-20.8% from Q1) followed by two consecutive quarters "
    "of growth, ending 31.7% above the Q2 low."
)


def _tool_call(tool: str, arguments: dict[str, object]) -> str:
    return json.dumps({"tool_calls": [{"name": tool, "arguments": arguments}]})


def _provider() -> MockProvider:
    return MockProvider(
        [
            MockRule(
                name="supervisor",
                match_system="supervisor",
                priority=10,
                responses=(
                    routing_decision(
                        "delegate",
                        agents=["data_agent"],
                        instructions=["Compute the total revenue across the four quarters."],
                        reason="a computation task needs data_agent's python_exec permission",
                    ),
                    routing_decision(
                        "delegate",
                        agents=["analyst_agent"],
                        instructions=[
                            "Compute the average and describe the quarter-over-quarter trend."
                        ],
                        reason="hand the total to the analyst for interpretation",
                    ),
                    routing_decision("finalize", answer=_FINAL_ANSWER),
                ),
            ),
            MockRule(
                name="data",
                match_request_key=":data_agent:",
                responses=(
                    _tool_call(
                        "python_exec",
                        {"code": "print(sum([120000, 95000, 143000, 158000]))"},
                    ),
                    agent_output(
                        f"Total quarterly revenue is ${_TOTAL:,}.", data={"total": _TOTAL}
                    ),
                ),
            ),
            MockRule(
                name="analyst",
                match_request_key=":analyst_agent:",
                responses=(
                    _tool_call("calculator", {"expression": f"{_TOTAL} / 4"}),
                    agent_output(_FINAL_ANSWER, confidence=0.9, data={"average": _AVERAGE}),
                ),
            ),
        ]
    )


async def main() -> None:
    args = common_arg_parser(__doc__ or "").parse_args()
    print_banner("Demo: Data Analysis")

    engine = await build_engine(
        test_db=args.test_db, provider=_provider(), sandbox_root=Path(__file__).parent / "workspace"
    )
    try:
        demo = await start_dynamic_run(
            engine, TASK, success_criteria=("computes a total and average",)
        )
        print(f"task: {TASK}\n")
        result = await demo.orchestrator.run(demo.state, demo.workflow)
        print_final(result.state)
    finally:
        await engine.aclose()


if __name__ == "__main__":
    run(main())
