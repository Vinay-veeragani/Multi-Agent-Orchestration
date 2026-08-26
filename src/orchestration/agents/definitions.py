"""Reference agent definitions.

These are *data*, not classes. Every agent below is an :class:`AgentDefinition`
that the shared agent runtime interprets; none of them subclass anything. That is
the point of the registry design -- an equivalent agent can be added over the HTTP
API without deploying code.

The tool allowlists here are the concrete answer to the permission requirement:

* ``research_agent`` may search and read, and cannot write, email, or execute.
* ``code_agent`` may read code and run tests, and has no database or shell access.
* ``data_agent`` may execute Python (it needs to), and cannot reach the network.
* ``analyst_agent`` / ``critic_agent`` / ``finalizer_agent`` reason over prior
  outputs and hold almost no tools at all -- a synthesiser that can call
  ``write_file`` is a synthesiser that can be talked into overwriting something.

Deny-by-default means the omissions are as deliberate as the inclusions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestration.domain.agent import AgentDefinition
from orchestration.domain.budget import Budget
from orchestration.domain.enums import ModelCapability
from orchestration.domain.model import RoutingCriteria
from orchestration.domain.retry import NETWORK_RETRY_POLICY, RetryPolicy
from orchestration.domain.tool import AgentCapability, ToolPermission

if TYPE_CHECKING:
    from orchestration.agents.registry import AgentRegistry

# ---------------------------------------------------------------------------
# Shared prompt scaffolding
# ---------------------------------------------------------------------------

#: Appended to every specialist prompt. Kept in one place so the output contract
#: cannot drift between agents -- the runtime parses this shape from all of them.
_OUTPUT_CONTRACT = """
Respond with a single JSON object and nothing else:
{
  "content": "your findings in prose",
  "confidence": 0.0-1.0,
  "claims": ["each distinct factual assertion you are making"],
  "evidence": ["a url, file path, or citation supporting the claims"],
  "gaps": ["anything you could not establish"],
  "data": {}
}

Rules you must follow:
- Never invent a source. If you did not retrieve it, it does not go in evidence.
- Set confidence below 0.5 when your evidence is thin, contradictory, or absent.
- Put what you could not determine in gaps rather than guessing.
- Every item in claims should be supported by an item in evidence.
""".strip()


def _prompt(role: str) -> str:
    return f"{role.strip()}\n\n{_OUTPUT_CONTRACT}"


# ---------------------------------------------------------------------------
# ResearchAgent
# ---------------------------------------------------------------------------

RESEARCH_AGENT = AgentDefinition(
    id="research_agent",
    name="Research Agent",
    description=(
        "Gathers information on a topic from search and local documents, "
        "summarises it, and extracts citations."
    ),
    kind="research",
    capabilities=(
        AgentCapability(
            name="web_research",
            description="Search external sources for information on a topic",
            keywords=frozenset(
                {
                    "research",
                    "search",
                    "find",
                    "investigate",
                    "gather",
                    "sources",
                    "who",
                    "what",
                    "background",
                    "landscape",
                    "vendors",
                    "companies",
                    "competitors",
                    "market",
                }
            ),
            proficiency=0.9,
        ),
        AgentCapability(
            name="source_collection",
            description="Collect and deduplicate source documents",
            keywords=frozenset({"sources", "citations", "references", "evidence"}),
            proficiency=0.85,
        ),
        AgentCapability(
            name="summarization",
            description="Condense long source material into findings",
            keywords=frozenset({"summarise", "summarize", "summary", "digest", "overview"}),
            proficiency=0.8,
        ),
        AgentCapability(
            name="citation_extraction",
            description="Attribute each claim to a retrieved source",
            keywords=frozenset({"cite", "citation", "attribute", "provenance"}),
            proficiency=0.85,
        ),
    ),
    # Read and search only. No write_file: a research agent that can write is a
    # research agent that can overwrite another node's artifact.
    allowed_tools=(
        ToolPermission(tool="web_search", max_calls=12),
        ToolPermission(
            tool="read_file",
            constraints={"path": {"prefix": "."}},
            reason="may read workspace documents provided with the task",
        ),
        ToolPermission(tool="http_request", max_calls=8, reason="fetch a cited page directly"),
    ),
    system_prompt=_prompt(
        """
You are a research specialist. Your job is to find what is actually true about
the topic you are given and to say where you learned it.

Method:
1. Search for the topic. Prefer several narrow queries over one broad one.
2. Read the results. If a result looks authoritative, fetch it.
3. Record a citation for every claim you make.
4. State what you could not find. An honest gap is worth more than a plausible
   guess, because a later agent will treat your output as evidence.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_USE}),
        prefer="balanced",
    ),
    timeout_seconds=120.0,
    retry_policy=NETWORK_RETRY_POLICY,
    max_iterations=6,
    confidence_floor=0.5,
    tags=frozenset({"reference", "research"}),
)


# ---------------------------------------------------------------------------
# DataAgent
# ---------------------------------------------------------------------------

DATA_AGENT = AgentDefinition(
    id="data_agent",
    name="Data Agent",
    description=(
        "Inspects tabular data, profiles schemas, computes statistics, assesses "
        "data quality, and produces charts."
    ),
    kind="data",
    capabilities=(
        AgentCapability(
            name="schema_analysis",
            description="Determine column names, types, and cardinality",
            keywords=frozenset({"schema", "columns", "dtype", "structure", "profile"}),
            proficiency=0.9,
        ),
        AgentCapability(
            name="statistics",
            description="Compute descriptive statistics and distributions",
            keywords=frozenset(
                {
                    "statistics",
                    "stats",
                    "mean",
                    "median",
                    "distribution",
                    "correlation",
                    "outlier",
                    "aggregate",
                    "average",
                }
            ),
            proficiency=0.9,
        ),
        AgentCapability(
            name="data_quality",
            description="Detect nulls, duplicates, and inconsistent values",
            keywords=frozenset({"quality", "missing", "null", "duplicate", "clean", "validate"}),
            proficiency=0.85,
        ),
        AgentCapability(
            name="tabular_inspection",
            description="Read CSV and Excel files",
            keywords=frozenset({"csv", "excel", "xlsx", "dataframe", "table", "dataset", "rows"}),
            proficiency=0.9,
        ),
        AgentCapability(
            name="charting",
            description="Render charts from data",
            keywords=frozenset({"chart", "plot", "graph", "visualise", "visualize", "histogram"}),
            proficiency=0.75,
        ),
    ),
    # Needs code execution to do real analysis; gets no network at all, so a
    # confused analysis step cannot exfiltrate the dataset it was handed.
    allowed_tools=(
        ToolPermission(tool="read_file", constraints={"path": {"prefix": "."}}),
        ToolPermission(tool="python_exec", max_calls=10, reason="perform the actual analysis"),
        ToolPermission(
            tool="write_file",
            constraints={"path": {"prefix": "analysis"}},
            reason="write charts and derived tables into the analysis directory only",
        ),
        ToolPermission(tool="calculator"),
    ),
    system_prompt=_prompt(
        """
You are a data analysis specialist working with tabular data.

Method:
1. Read the file you were given and establish its real schema -- do not assume
   column names or types.
2. Profile it: row count, null counts, duplicates, obvious anomalies.
3. Compute the statistics the task actually asks for, using python_exec.
4. Put concrete numbers in `data` so downstream agents can use them without
   re-reading the file.

Report the numbers you computed, not impressions of them. If the data is too
dirty to answer the question, say so in gaps.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_USE, ModelCapability.REASONING}
        ),
        prefer="balanced",
    ),
    timeout_seconds=180.0,
    max_iterations=8,
    confidence_floor=0.6,
    tags=frozenset({"reference", "data"}),
)


# ---------------------------------------------------------------------------
# CodeAgent
# ---------------------------------------------------------------------------

CODE_AGENT = AgentDefinition(
    id="code_agent",
    name="Code Agent",
    description=(
        "Inspects a repository: reads files, searches code, and runs the test "
        "suite to verify behaviour."
    ),
    kind="code",
    capabilities=(
        AgentCapability(
            name="repository_inspection",
            description="Navigate a codebase and read its files",
            keywords=frozenset(
                {"repository", "repo", "codebase", "module", "package", "source", "file"}
            ),
            proficiency=0.85,
        ),
        AgentCapability(
            name="code_search",
            description="Locate symbols and usages in source code",
            keywords=frozenset({"code", "function", "class", "grep", "search", "usage", "symbol"}),
            proficiency=0.85,
        ),
        AgentCapability(
            name="test_execution",
            description="Run tests and interpret the results",
            keywords=frozenset({"test", "tests", "pytest", "failing", "suite", "coverage"}),
            proficiency=0.8,
        ),
    ),
    # Explicitly no exec_shell and no db_query. Test execution goes through
    # python_exec, which is bounded and does not accept a free-form command line.
    allowed_tools=(
        ToolPermission(tool="read_file", constraints={"path": {"prefix": "."}}),
        ToolPermission(
            tool="write_file",
            constraints={"path": {"prefix": "scratch"}},
            reason="may write scratch files only, never source",
        ),
        ToolPermission(
            tool="python_exec",
            max_calls=6,
            reason="run the test suite; deliberately not exec_shell",
        ),
    ),
    system_prompt=_prompt(
        """
You are a code inspection specialist.

Method:
1. Locate the relevant files before reading them; do not guess at paths.
2. Quote the code you are reasoning about, with its file path.
3. When a claim about behaviour can be checked by running the tests, run them
   and report what actually happened.

You cannot run arbitrary shell commands and you cannot modify source files. If
answering would require either, say so in gaps.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_USE, ModelCapability.REASONING}
        ),
        prefer="most_capable",
    ),
    timeout_seconds=180.0,
    max_iterations=8,
    confidence_floor=0.6,
    tags=frozenset({"reference", "code"}),
)


# ---------------------------------------------------------------------------
# AnalystAgent
# ---------------------------------------------------------------------------

ANALYST_AGENT = AgentDefinition(
    id="analyst_agent",
    name="Analyst Agent",
    description=(
        "Compares and aggregates the outputs of other agents into a coherent "
        "analysis, reconciling disagreements between them."
    ),
    kind="analyst",
    capabilities=(
        AgentCapability(
            name="comparison",
            description="Compare options across shared dimensions",
            keywords=frozenset(
                {"compare", "comparison", "versus", "vs", "differences", "ranking", "best"}
            ),
            proficiency=0.9,
        ),
        AgentCapability(
            name="aggregation",
            description="Combine findings from several sources",
            keywords=frozenset({"aggregate", "combine", "consolidate", "merge", "synthesis"}),
            proficiency=0.85,
        ),
        AgentCapability(
            name="reasoning_over_outputs",
            description="Reason over prior agent outputs",
            keywords=frozenset({"analyse", "analyze", "analysis", "assess", "evaluate", "why"}),
            proficiency=0.85,
        ),
    ),
    # Reasons over what it is given. A calculator is genuinely useful for
    # normalising prices; nothing else is, so nothing else is granted.
    allowed_tools=(ToolPermission(tool="calculator"),),
    system_prompt=_prompt(
        """
You are an analyst. You are given the outputs of other agents and must turn them
into one coherent picture.

Method:
1. Build the comparison the task asks for, dimension by dimension.
2. Where two inputs disagree, say so explicitly and state which you find more
   credible and why -- do not silently average them.
3. Carry the upstream citations through into your evidence. You are not the
   origin of these facts and should not present yourself as such.
4. Where an input left a gap, the gap survives into your output.

You have no research tools. If the inputs are insufficient, the correct answer
is a low confidence score and a clear list of what is missing.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.REASONING}),
        prefer="most_capable",
    ),
    timeout_seconds=120.0,
    max_iterations=3,
    confidence_floor=0.6,
    tags=frozenset({"reference", "synthesis"}),
)


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

CRITIC_AGENT = AgentDefinition(
    id="critic_agent",
    name="Critic Agent",
    description=(
        "Audits another agent's output for unsupported claims, missing evidence, "
        "and internal contradictions, and scores its quality."
    ),
    kind="critic",
    capabilities=(
        AgentCapability(
            name="claim_verification",
            description="Check whether claims are supported by cited evidence",
            keywords=frozenset({"verify", "validate", "check", "unsupported", "substantiate"}),
            proficiency=0.9,
        ),
        AgentCapability(
            name="contradiction_detection",
            description="Find statements that conflict with each other",
            keywords=frozenset({"contradiction", "inconsistent", "conflict", "disagree"}),
            proficiency=0.85,
        ),
        AgentCapability(
            name="quality_scoring",
            description="Score output quality against the task criteria",
            keywords=frozenset({"critique", "review", "quality", "score", "audit", "rigour"}),
            proficiency=0.85,
        ),
    ),
    allowed_tools=(
        ToolPermission(
            tool="web_search",
            max_calls=4,
            reason="spot-check a suspicious claim against an independent source",
        ),
    ),
    system_prompt=_prompt(
        """
You are a critic. Your job is to find what is wrong with the work you are given.
You are not being asked to be agreeable.

Check for:
1. Claims with no supporting evidence.
2. Evidence that does not actually support the claim it is attached to.
3. Statements that contradict each other or contradict a cited source.
4. Confident language covering a thin factual basis.
5. Success criteria the task set that the work does not meet.

Put each specific problem in `claims` as a finding, and set `confidence` to your
confidence in the *audit*, not in the work. Use `data.quality_score` (0.0-1.0)
for your assessment of the work itself, and `data.blocking` (true/false) for
whether the problems are severe enough that the work should not be accepted.

If the work is genuinely sound, say so plainly rather than inventing objections.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.REASONING}),
        prefer="most_capable",
    ),
    timeout_seconds=120.0,
    max_iterations=4,
    confidence_floor=0.6,
    tags=frozenset({"reference", "quality"}),
)


# ---------------------------------------------------------------------------
# FinalizerAgent
# ---------------------------------------------------------------------------

FINALIZER_AGENT = AgentDefinition(
    id="finalizer_agent",
    name="Finalizer Agent",
    description=(
        "Synthesises all prior outputs into the final structured deliverable for the user."
    ),
    kind="finalizer",
    capabilities=(
        AgentCapability(
            name="synthesis",
            description="Merge all findings into one answer",
            keywords=frozenset({"final", "finalise", "finalize", "synthesise", "deliverable"}),
            proficiency=0.9,
        ),
        AgentCapability(
            name="report_generation",
            description="Produce a structured report",
            keywords=frozenset({"report", "write-up", "document", "summary", "brief"}),
            proficiency=0.9,
        ),
        AgentCapability(
            name="formatting",
            description="Format output to a required structure",
            keywords=frozenset({"format", "structure", "markdown", "table", "present"}),
            proficiency=0.85,
        ),
    ),
    allowed_tools=(
        ToolPermission(
            tool="write_file",
            constraints={"path": {"prefix": "reports"}},
            reason="write the final report into the reports directory only",
        ),
    ),
    system_prompt=_prompt(
        """
You are the finalizer. You produce the answer the user actually receives.

Method:
1. Answer the original question directly, in the first sentence.
2. Support it with the findings from earlier agents, keeping their citations.
3. Include the comparison, table, or structure the task asked for.
4. State remaining uncertainty in one short section. Do not bury it and do not
   omit it -- a confident report over unresolved gaps is a worse deliverable
   than an honest one.
5. If the critic raised blocking problems that were never resolved, say so.

Never introduce a fact that no upstream agent established.
        """
    ),
    routing_criteria=RoutingCriteria(
        required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.REASONING}),
        prefer="most_capable",
    ),
    timeout_seconds=150.0,
    max_iterations=3,
    confidence_floor=0.5,
    tags=frozenset({"reference", "synthesis"}),
)


# ---------------------------------------------------------------------------
# Derived specialists
# ---------------------------------------------------------------------------


def _research_variant(
    *,
    agent_id: str,
    name: str,
    description: str,
    focus: str,
    keywords: frozenset[str],
) -> AgentDefinition:
    """Derive a focused research agent from :data:`RESEARCH_AGENT`.

    The parallel-execution demo needs three researchers working different
    dimensions concurrently. Deriving them from one definition -- rather than
    writing three near-identical classes -- is exactly the reuse the
    definition-as-data design is for.
    """
    return RESEARCH_AGENT.merged(
        id=agent_id,
        name=name,
        description=description,
        capabilities=tuple(
            cap.model_dump()
            for cap in (
                AgentCapability(
                    name=f"{agent_id}_focus",
                    description=focus,
                    keywords=keywords,
                    proficiency=0.9,
                ),
                *RESEARCH_AGENT.capabilities,
            )
        ),
        system_prompt=f"{RESEARCH_AGENT.system_prompt}\n\nYour specific focus: {focus}",
        tags=frozenset({"reference", "research", "derived"}),
    )


PRICING_AGENT = _research_variant(
    agent_id="pricing_agent",
    name="Pricing Research Agent",
    description="Researches pricing, plans, tiers, and total cost of ownership.",
    focus=(
        "Find concrete prices with their unit and billing period. A price without "
        "a unit is not an answer."
    ),
    keywords=frozenset(
        {"price", "pricing", "cost", "tier", "plan", "seat", "licence", "license", "subscription"}
    ),
)

FEATURE_AGENT = _research_variant(
    agent_id="feature_agent",
    name="Feature Research Agent",
    description="Researches product features and AI capabilities.",
    focus=(
        "Find what the product can actually do, especially its AI capabilities. "
        "Distinguish shipped features from announced roadmap."
    ),
    keywords=frozenset(
        {"feature", "features", "capability", "capabilities", "ai", "functionality", "integration"}
    ),
)


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------

#: Every reference agent, in a stable order.
REFERENCE_AGENTS: tuple[AgentDefinition, ...] = (
    RESEARCH_AGENT,
    PRICING_AGENT,
    FEATURE_AGENT,
    DATA_AGENT,
    CODE_AGENT,
    ANALYST_AGENT,
    CRITIC_AGENT,
    FINALIZER_AGENT,
)

#: A tight budget applied to derived research agents in the parallel demo, so one
#: runaway branch cannot consume the whole execution allowance.
BRANCH_BUDGET = Budget(
    max_cost_usd=0.10,
    max_tokens=12_000,
    max_duration_seconds=90.0,
    max_agent_steps=6,
    max_tool_calls=15,
    max_retries=3,
)


def build_default_agent_registry() -> AgentRegistry:
    """Construct an :class:`AgentRegistry` populated with the reference agents."""
    from orchestration.agents.registry import AgentRegistry

    registry = AgentRegistry()
    registry.register_all(REFERENCE_AGENTS)
    return registry


__all__ = [
    "ANALYST_AGENT",
    "BRANCH_BUDGET",
    "CODE_AGENT",
    "CRITIC_AGENT",
    "DATA_AGENT",
    "FEATURE_AGENT",
    "FINALIZER_AGENT",
    "PRICING_AGENT",
    "REFERENCE_AGENTS",
    "RESEARCH_AGENT",
    "RetryPolicy",
    "build_default_agent_registry",
]
