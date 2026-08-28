#!/usr/bin/env python
"""Demo: competitive intelligence.

Task: compare the top CRM vendors on pricing and AI features, then produce one
synthesised report. Shows the engine's parallel fan-out (three independent
researchers at once) followed by sequential synthesis -- the shape most
research-style multi-agent tasks actually take.

Run from the repository root::

    python examples/research/run.py
    python examples/research/run.py --test-db
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
    "Compare Salesforce, HubSpot, and Zoho on pricing and AI features, then "
    "write a one-page recommendation for a 50-person sales team."
)

_FINAL_REPORT = """\
# CRM Vendor Recommendation

**Pricing.** Salesforce ($25-300/seat/mo) and HubSpot ($20-150/seat/mo) both \
scale steeply with tier; Zoho ($14-52/seat/mo) is the clear value leader at \
every tier we compared.

**AI features.** Salesforce's Einstein Copilot is the most mature offering, \
with deep pipeline-forecasting integration. HubSpot's Breeze AI covers content \
and lead-scoring well. Zoho's Zia is capable but noticeably behind on \
generative features.

**Recommendation.** For a 50-person team prioritising cost predictability, \
Zoho is the strongest fit; teams that lean heavily on AI-assisted forecasting \
should budget for Salesforce despite the premium.
"""


def _provider() -> MockProvider:
    return MockProvider(
        [
            MockRule(
                name="supervisor",
                match_system="supervisor",
                priority=10,
                responses=(
                    routing_decision(
                        "parallel_delegate",
                        agents=["research_agent", "pricing_agent", "feature_agent"],
                        reason="three independent research dimensions can run at once",
                    ),
                    routing_decision(
                        "delegate",
                        agents=["analyst_agent"],
                        instructions=[
                            "Synthesise the three research findings into one comparison."
                        ],
                        reason="synthesise the parallel findings",
                    ),
                    routing_decision(
                        "delegate",
                        agents=["finalizer_agent"],
                        instructions=["Write the final one-page recommendation."],
                        reason="produce the final deliverable",
                    ),
                    routing_decision("finalize", answer=_FINAL_REPORT),
                ),
            ),
            MockRule(
                name="research",
                match_request_key=":research_agent:",
                responses=(
                    agent_output(
                        "Salesforce, HubSpot, and Zoho are the three leading CRM platforms "
                        "for mid-market sales teams, all offering native AI copilots.",
                        evidence=["https://example.test/crm-market-2026"],
                    ),
                ),
            ),
            MockRule(
                name="pricing",
                match_request_key=":pricing_agent:",
                responses=(
                    agent_output(
                        "Salesforce: $25-300/seat/mo. HubSpot: $20-150/seat/mo. "
                        "Zoho: $14-52/seat/mo.",
                        evidence=["https://example.test/pricing-pages"],
                    ),
                ),
            ),
            MockRule(
                name="feature",
                match_request_key=":feature_agent:",
                responses=(
                    agent_output(
                        "Salesforce's Einstein Copilot leads on pipeline forecasting. "
                        "HubSpot's Breeze AI is strong on content/lead-scoring. "
                        "Zoho's Zia trails on generative features.",
                        evidence=["https://example.test/ai-feature-comparison"],
                    ),
                ),
            ),
            MockRule(
                name="analyst",
                match_request_key=":analyst_agent:",
                responses=(
                    agent_output(
                        "Zoho wins on value; Salesforce wins on AI maturity at a premium; "
                        "HubSpot is the balanced middle option.",
                        confidence=0.85,
                    ),
                ),
            ),
            MockRule(
                name="finalizer",
                match_request_key=":finalizer_agent:",
                responses=(agent_output(_FINAL_REPORT, confidence=0.9),),
            ),
        ]
    )


async def main() -> None:
    args = common_arg_parser(__doc__ or "").parse_args()
    print_banner("Demo: Competitive Intelligence")

    engine = await build_engine(
        test_db=args.test_db, provider=_provider(), sandbox_root=Path(__file__).parent / "workspace"
    )
    try:
        demo = await start_dynamic_run(
            engine, TASK, success_criteria=("names at least 3 vendors", "cites a price per vendor")
        )
        print(f"task: {TASK}\n")
        result = await demo.orchestrator.run(demo.state, demo.workflow)
        print_final(result.state)
    finally:
        await engine.aclose()


if __name__ == "__main__":
    run(main())
