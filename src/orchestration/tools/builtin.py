"""Reference tool implementations.

These exist to exercise the tool contract across the full risk spectrum -- from a
pure calculator to shell execution -- not to be a comprehensive toolbox.

Risk assignments drive the policy engine, so they are stated deliberately:

============== ========== ====================================================
Tool           Risk       Rationale
============== ========== ====================================================
calculator     SAFE       Pure arithmetic, no I/O.
web_search     LOW        Outbound read; mocked by default.
read_file      SAFE       Read confined to the sandbox root.
write_file     MEDIUM     Local mutation inside the sandbox.
http_request   LOW/HIGH   GET is a read; other verbs mutate remote state.
python_exec    MEDIUM     Runs code in a subprocess. **Not a sandbox.**
db_query       MEDIUM     Read-only SQL; writes are rejected outright.
send_email     HIGH       Externally visible, irreversible. Approval-gated.
exec_shell     CRITICAL   Arbitrary command execution. Disabled by default.
============== ========== ====================================================

The security limitations of ``python_exec`` and ``exec_shell`` are real and are
documented in ``docs/security.md`` rather than being papered over here.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import operator
import re
import sys
from collections.abc import Callable
from typing import Any, ClassVar, Final

import httpx

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import RiskLevel
from orchestration.domain.retry import NETWORK_RETRY_POLICY, NO_RETRY_POLICY, RetryPolicy
from orchestration.domain.tool import ToolSpec
from orchestration.errors import (
    InputValidationError,
    NetworkError,
    PolicyViolationError,
    ProviderUnavailableError,
    RateLimitError,
)
from orchestration.tools.base import Tool, ToolContext, object_schema

# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------

#: Operators permitted in a calculator expression.
_BINARY_OPS: Final[dict[type[ast.operator], Callable[[Any, Any], Any]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: Final[dict[type[ast.unaryop], Callable[[Any], Any]]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

#: Cap on exponentiation, so ``9**9**9`` cannot hang the event loop.
_MAX_EXPONENT: Final[int] = 64


class CalculatorTool(Tool):
    """Evaluate an arithmetic expression.

    Implemented as an AST walk over an explicit operator allowlist rather than
    ``eval``. ``eval`` on model-supplied text is arbitrary code execution; there
    is no amount of input filtering that makes it acceptable here.
    """

    spec = ToolSpec(
        name="calculator",
        description=(
            "Evaluate an arithmetic expression and return its numeric value. "
            "Supports + - * / // % ** and parentheses."
        ),
        input_schema=object_schema(
            {"expression": {"type": "string", "minLength": 1, "maxLength": 500}},
            required=["expression"],
        ),
        output_schema=object_schema({"value": {"type": "number"}}, required=["value"]),
        risk=RiskLevel.SAFE,
        timeout_seconds=5.0,
        tags=frozenset({"math", "pure", "read"}),
    )

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        expression = str(arguments["expression"])
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise InputValidationError(
                "expression is not valid arithmetic syntax",
                expression=expression,
                detail=str(exc),
            ) from exc
        value = self._evaluate(tree.body)
        return {"value": value, "expression": expression}

    def _evaluate(self, node: ast.expr) -> float:
        """Recursively evaluate an allowlisted arithmetic AST."""
        match node:
            case ast.Constant(value=bool()):
                # bool is an int subclass; rejecting it keeps the contract numeric.
                raise InputValidationError("booleans are not valid in an arithmetic expression")
            case ast.Constant(value=int() | float() as value):
                return float(value)
            case ast.BinOp(left=left, op=op, right=right):
                handler = _BINARY_OPS.get(type(op))
                if handler is None:
                    raise InputValidationError(f"operator {type(op).__name__} is not permitted")
                lhs, rhs = self._evaluate(left), self._evaluate(right)
                if isinstance(op, ast.Pow) and abs(rhs) > _MAX_EXPONENT:
                    raise InputValidationError(
                        f"exponent {rhs} exceeds the maximum of {_MAX_EXPONENT}",
                    )
                try:
                    return float(handler(lhs, rhs))
                except ZeroDivisionError as exc:
                    raise InputValidationError("division by zero") from exc
                except OverflowError as exc:
                    raise InputValidationError("arithmetic overflow") from exc
            case ast.UnaryOp(op=op, operand=operand):
                unary = _UNARY_OPS.get(type(op))
                if unary is None:
                    raise InputValidationError(f"unary {type(op).__name__} is not permitted")
                return float(unary(self._evaluate(operand)))
            case _:
                raise InputValidationError(
                    f"expression element {type(node).__name__} is not permitted",
                )


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


class WebSearchTool(Tool):
    """Search abstraction with a deterministic offline corpus.

    Real search requires a paid API key that CI will not have. Rather than
    skipping the capability, this ships a fixed corpus so the demos and the
    benchmark are reproducible, and documents the substitution in its own
    result payload (``source: "offline_corpus"``) so no caller can mistake the
    data for live results.
    """

    spec = ToolSpec(
        name="web_search",
        description=(
            "Search for information on a topic. Returns a ranked list of results "
            "with title, url and snippet."
        ),
        input_schema=object_schema(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                # `additionalProperties: false` (object_schema's default) means a
                # provider that validates tool-call arguments strictly -- Groq
                # does, for at least gpt-oss-120b -- rejects the call outright if
                # the model includes a parameter this schema does not declare.
                # These three are exactly what that model tends to invent
                # unprompted for a search tool; declaring them keeps the call
                # from failing before it ever reaches `run`. `recency_days` and
                # `source` are accepted but not applied -- this is a fixed
                # offline corpus (see the class docstring), which carries no
                # publish date or source metadata to filter by, and pretending
                # otherwise would misrepresent what the result actually is.
                "topn": {"type": "integer", "minimum": 1, "maximum": 20},
                "recency_days": {"type": "integer"},
                "source": {"type": "string"},
            },
            required=["query"],
        ),
        risk=RiskLevel.LOW,
        timeout_seconds=20.0,
        retry_policy=NETWORK_RETRY_POLICY,
        tags=frozenset({"research", "read", "external"}),
    )

    def __init__(self, corpus: dict[str, list[JsonDict]] | None = None) -> None:
        self._corpus = corpus if corpus is not None else _DEFAULT_SEARCH_CORPUS
        super().__init__()

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        query = str(arguments["query"])
        # `topn` is the same idea as `max_results` under a different name a
        # model sometimes reaches for; honour whichever was actually given.
        limit = int(arguments.get("max_results") or arguments.get("topn") or 5)
        results = self._match(query)[:limit]
        return {
            "query": query,
            "source": "offline_corpus",
            "result_count": len(results),
            "results": results,
        }

    def _match(self, query: str) -> list[JsonDict]:
        """Rank corpus entries by term overlap.

        A deliberately simple scorer: the point is determinism, not retrieval
        quality. Semantic retrieval is handled by the pgvector-backed evidence
        store, which is a separate concern.
        """
        terms = {t for t in re.split(r"\W+", query.lower()) if len(t) > 2}
        scored: list[tuple[int, JsonDict]] = []
        for topic, entries in self._corpus.items():
            topic_terms = set(re.split(r"\W+", topic.lower()))
            for entry in entries:
                haystack = f"{entry['title']} {entry['snippet']}".lower()
                score = sum(1 for t in terms if t in haystack)
                score += 2 * len(terms & topic_terms)
                if score:
                    scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1]["title"])))
        return [entry for _, entry in scored]


#: Fixed corpus backing the CRM competitive-intelligence demo and benchmark.
_DEFAULT_SEARCH_CORPUS: Final[dict[str, list[JsonDict]]] = {
    "crm vendors": [
        {
            "title": "Salesforce Sales Cloud overview",
            "url": "https://example.test/salesforce/sales-cloud",
            "snippet": (
                "Salesforce Sales Cloud is a CRM platform. Editions: Starter, Pro, "
                "Enterprise, Unlimited. Einstein AI provides lead scoring and "
                "conversation insights."
            ),
            "published": "2025-11-02",
        },
        {
            "title": "HubSpot Sales Hub pricing and features",
            "url": "https://example.test/hubspot/sales-hub",
            "snippet": (
                "HubSpot Sales Hub offers Free, Starter, Professional and Enterprise "
                "tiers. Breeze AI adds prospecting agents and content assistance."
            ),
            "published": "2025-10-18",
        },
        {
            "title": "Microsoft Dynamics 365 Sales capabilities",
            "url": "https://example.test/microsoft/dynamics-365-sales",
            "snippet": (
                "Dynamics 365 Sales integrates with Microsoft 365. Copilot provides "
                "meeting summaries and pipeline forecasting."
            ),
            "published": "2025-09-30",
        },
        {
            "title": "Zoho CRM feature comparison",
            "url": "https://example.test/zoho/crm",
            "snippet": (
                "Zoho CRM spans Standard, Professional, Enterprise and Ultimate "
                "editions. Zia is the built-in AI assistant for anomaly detection."
            ),
            "published": "2025-08-11",
        },
        {
            "title": "Pipedrive for small sales teams",
            "url": "https://example.test/pipedrive/overview",
            "snippet": (
                "Pipedrive is a pipeline-first CRM with Essential, Advanced, "
                "Professional and Enterprise plans. AI sales assistant surfaces "
                "next-best actions."
            ),
            "published": "2025-07-22",
        },
    ],
    "crm pricing": [
        {
            "title": "Salesforce Sales Cloud list pricing",
            "url": "https://example.test/salesforce/pricing",
            "snippet": (
                "Starter 25 USD per user per month; Pro 100; Enterprise 165; "
                "Unlimited 330. Billed annually."
            ),
            "published": "2025-11-02",
        },
        {
            "title": "HubSpot Sales Hub price list",
            "url": "https://example.test/hubspot/pricing",
            "snippet": (
                "Free 0 USD; Starter 20 USD per seat per month; Professional 100; Enterprise 150."
            ),
            "published": "2025-10-18",
        },
        {
            "title": "Dynamics 365 Sales pricing",
            "url": "https://example.test/microsoft/pricing",
            "snippet": "Professional 65 USD per user per month; Enterprise 95; Premium 135.",
            "published": "2025-09-30",
        },
        {
            "title": "Zoho CRM pricing tiers",
            "url": "https://example.test/zoho/pricing",
            "snippet": "Standard 14 EUR; Professional 23; Enterprise 40; Ultimate 52 per user.",
            "published": "2025-08-11",
        },
        {
            "title": "Pipedrive plan pricing",
            "url": "https://example.test/pipedrive/pricing",
            "snippet": "Essential 14 USD; Advanced 29; Professional 59; Enterprise 99 per seat.",
            "published": "2025-07-22",
        },
    ],
    "crm ai capabilities": [
        {
            "title": "Einstein AI in Salesforce",
            "url": "https://example.test/salesforce/einstein",
            "snippet": (
                "Einstein provides predictive lead scoring, opportunity insights, "
                "and an Agentforce agent builder."
            ),
            "published": "2025-11-02",
        },
        {
            "title": "HubSpot Breeze AI",
            "url": "https://example.test/hubspot/breeze",
            "snippet": (
                "Breeze includes a prospecting agent, content agent, and customer "
                "agent, plus AI-assisted forecasting."
            ),
            "published": "2025-10-18",
        },
        {
            "title": "Copilot in Dynamics 365",
            "url": "https://example.test/microsoft/copilot",
            "snippet": (
                "Copilot drafts emails, summarises meetings and produces pipeline "
                "forecasts using Azure OpenAI."
            ),
            "published": "2025-09-30",
        },
    ],
}


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


class ReadFileTool(Tool):
    """Read a UTF-8 text file from inside the sandbox."""

    spec = ToolSpec(
        name="read_file",
        description="Read the contents of a text file. Paths are relative to the workspace root.",
        input_schema=object_schema(
            {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 5_000_000},
            },
            required=["path"],
        ),
        risk=RiskLevel.SAFE,
        timeout_seconds=10.0,
        tags=frozenset({"filesystem", "read"}),
    )

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        target = context.resolve_in_sandbox(str(arguments["path"]))
        max_bytes = int(arguments.get("max_bytes", 200_000))
        if not target.exists():
            raise InputValidationError("file does not exist", path=str(arguments["path"]))
        if not target.is_file():
            raise InputValidationError("path is not a regular file", path=str(arguments["path"]))

        # Reads happen off the event loop: a large file on a slow disk would
        # otherwise block every other concurrent agent.
        def _read() -> tuple[str, int, bool]:
            size = target.stat().st_size
            data = target.read_bytes()[:max_bytes]
            return data.decode("utf-8", errors="replace"), size, size > max_bytes

        content, size, truncated = await asyncio.to_thread(_read)
        return {
            "path": str(target.relative_to(context.sandbox_root.resolve())),
            "content": content,
            "size_bytes": size,
            "truncated": truncated,
        }


class WriteFileTool(Tool):
    """Write a UTF-8 text file inside the sandbox.

    Not idempotent in the general case -- an append changes state each time -- so
    it declares :data:`NO_RETRY_POLICY`, which the ``ToolSpec`` validator
    requires.
    """

    spec = ToolSpec(
        name="write_file",
        description="Write text to a file inside the workspace. Creates parent directories.",
        input_schema=object_schema(
            {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "content": {"type": "string", "maxLength": 5_000_000},
                "mode": {"type": "string", "enum": ["overwrite", "append"]},
            },
            required=["path", "content"],
        ),
        risk=RiskLevel.MEDIUM,
        idempotent=False,
        retry_policy=NO_RETRY_POLICY,
        timeout_seconds=15.0,
        tags=frozenset({"filesystem", "write"}),
    )

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        target = context.resolve_in_sandbox(str(arguments["path"]))
        content = str(arguments["content"])
        append = arguments.get("mode") == "append"

        def _write() -> int:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a" if append else "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            return target.stat().st_size

        size = await asyncio.to_thread(_write)
        return {
            "path": str(target.relative_to(context.sandbox_root.resolve())),
            "bytes_written": len(content.encode("utf-8")),
            "size_bytes": size,
            "mode": "append" if append else "overwrite",
        }


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------

#: Hostnames that must never be reachable: cloud metadata and loopback.
_BLOCKED_HOST_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^169\.254\.169\.254$"),  # AWS/GCP/Azure instance metadata
    re.compile(r"^metadata\.google\.internal$"),
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"^127\."),
    re.compile(r"^0\.0\.0\.0$"),
    re.compile(r"^\[?::1\]?$"),
    re.compile(r"^10\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),
)


class HttpRequestTool(Tool):
    """Perform an outbound HTTP request.

    Blocks link-local metadata endpoints and private address ranges by default.
    This is SSRF mitigation by denylist, which is genuinely weaker than an egress
    proxy -- a DNS name resolving to a private address still gets through. That
    limitation is stated in ``docs/security.md`` rather than implied to be solved.
    """

    spec = ToolSpec(
        name="http_request",
        description=(
            "Perform an HTTP request to a public URL and return status, headers "
            "and body. Only GET and HEAD are permitted by default."
        ),
        input_schema=object_schema(
            {
                "url": {"type": "string", "minLength": 8, "maxLength": 2048},
                "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "DELETE"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string", "maxLength": 100_000},
                "max_response_bytes": {"type": "integer", "minimum": 1, "maximum": 5_000_000},
            },
            required=["url"],
        ),
        risk=RiskLevel.LOW,
        timeout_seconds=30.0,
        retry_policy=NETWORK_RETRY_POLICY,
        tags=frozenset({"network", "read", "external"}),
    )

    #: Verbs that mutate remote state; permitted only when explicitly allowed.
    MUTATING_METHODS: ClassVar[frozenset[str]] = frozenset({"POST", "PUT", "DELETE", "PATCH"})

    def __init__(self, *, allow_mutating_methods: bool = False) -> None:
        self._allow_mutating = allow_mutating_methods
        super().__init__()

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        url = str(arguments["url"])
        method = str(arguments.get("method", "GET")).upper()
        limit = int(arguments.get("max_response_bytes", 200_000))

        self._check_url(url)
        if method in self.MUTATING_METHODS and not self._allow_mutating:
            raise PolicyViolationError(
                f"HTTP {method} is not permitted by this tool instance",
                method=method,
                hint="register http_request with allow_mutating_methods=True to enable",
            )

        try:
            async with httpx.AsyncClient(
                timeout=self.spec.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=dict(arguments.get("headers") or {}),
                    content=arguments.get("body"),
                )
        except httpx.TimeoutException as exc:
            raise NetworkError("http request timed out", url=url) from exc
        except httpx.HTTPError as exc:
            raise NetworkError("http request failed", url=url, detail=str(exc)) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitError(
                "upstream returned 429",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
                url=url,
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"upstream returned {response.status_code}",
                url=url,
                status_code=response.status_code,
            )

        body = response.content[:limit]
        return {
            "url": url,
            "method": method,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body.decode("utf-8", errors="replace"),
            "truncated": len(response.content) > limit,
        }

    @staticmethod
    def _check_url(url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise InputValidationError(
                "only http and https urls are permitted", scheme=parsed.scheme
            )
        host = (parsed.hostname or "").strip()
        if not host:
            raise InputValidationError("url has no host", url=url)
        for pattern in _BLOCKED_HOST_PATTERNS:
            if pattern.match(host):
                raise PolicyViolationError(
                    "requests to loopback, private, or metadata addresses are blocked",
                    host=host,
                )


# ---------------------------------------------------------------------------
# python_exec
# ---------------------------------------------------------------------------


class PythonExecTool(Tool):
    """Execute a Python snippet in a subprocess.

    **This is isolation, not a sandbox.** The subprocess runs with the same user
    and filesystem access as the engine. It is bounded by a timeout and an output
    cap, and it is started with ``-I`` (isolated mode) so it ignores the user
    site directory and ``PYTHON*`` environment variables -- but a snippet can
    still read files the engine can read and open network connections.

    It is included because data-analysis agents genuinely need it, and disabling
    it entirely would make the data-analysis demo dishonest. The real control is
    the permission system: only agents whose allowlist includes it can call it.
    """

    spec = ToolSpec(
        name="python_exec",
        description=(
            "Execute a short Python script and capture stdout. Use for data "
            "analysis and computation. The script has a time limit."
        ),
        input_schema=object_schema(
            {
                "code": {"type": "string", "minLength": 1, "maxLength": 50_000},
                "stdin": {"type": "string", "maxLength": 100_000},
            },
            required=["code"],
        ),
        risk=RiskLevel.MEDIUM,
        idempotent=False,
        retry_policy=NO_RETRY_POLICY,
        timeout_seconds=30.0,
        tags=frozenset({"compute", "code"}),
    )

    #: Cap on captured output so a runaway loop cannot exhaust memory.
    MAX_OUTPUT_BYTES: ClassVar[int] = 200_000

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds
        super().__init__()

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        code = str(arguments["code"])
        stdin_data = str(arguments.get("stdin", ""))
        timeout = min(self._timeout, context.deadline_seconds)

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: ignore user site-packages and PYTHON* env vars
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(context.sandbox_root.resolve()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_data.encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            # Kill rather than terminate: a snippet can trap SIGTERM.
            process.kill()
            await process.wait()
            raise
        return {
            "exit_code": process.returncode,
            "stdout": stdout[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "timed_out": False,
        }


# ---------------------------------------------------------------------------
# db_query
# ---------------------------------------------------------------------------

#: SQL keywords that indicate a mutation or a schema change.
_WRITE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "truncate",
        "alter",
        "create",
        "grant",
        "revoke",
        "copy",
        "vacuum",
        "reindex",
        "call",
        "do",
    }
)


class DatabaseQueryTool(Tool):
    """Run a read-only SQL query against the engine's PostgreSQL database.

    Writes are rejected by keyword inspection *and* the query runs in a
    ``READ ONLY`` transaction. The transaction is the real control -- keyword
    matching alone is defeatable -- but the keyword check gives a clear error
    before a round trip.
    """

    spec = ToolSpec(
        name="db_query",
        description=(
            "Run a read-only SQL SELECT against the orchestration database and "
            "return rows. Mutating statements are rejected."
        ),
        input_schema=object_schema(
            {
                "sql": {"type": "string", "minLength": 1, "maxLength": 10_000},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 1_000},
            },
            required=["sql"],
        ),
        risk=RiskLevel.MEDIUM,
        timeout_seconds=20.0,
        retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0.25, jitter="full"),
        tags=frozenset({"database", "read"}),
    )

    def __init__(self, session_factory: Any | None = None) -> None:
        """Args:
        session_factory: Async SQLAlchemy session factory. Injected rather
            than imported so the tool can be unit-tested without a database
            and so it cannot reach for a connection it was not given.
        """
        self._session_factory = session_factory
        super().__init__()

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        sql = str(arguments["sql"]).strip()
        max_rows = int(arguments.get("max_rows", 100))
        self._reject_writes(sql)

        if self._session_factory is None:
            raise PolicyViolationError(
                "db_query was registered without a database session factory",
                hint="pass session_factory when constructing DatabaseQueryTool",
            )

        from sqlalchemy import text

        async with self._session_factory() as session:
            # A read-only transaction is the enforcement; the keyword check above
            # only produces a friendlier error earlier.
            await session.execute(text("SET TRANSACTION READ ONLY"))
            result = await session.execute(text(sql))
            rows = result.mappings().fetchmany(max_rows)
            return {
                "sql": sql,
                "row_count": len(rows),
                "columns": list(rows[0].keys()) if rows else [],
                "rows": [json.loads(json.dumps(dict(r), default=str)) for r in rows],
            }

    @staticmethod
    def _reject_writes(sql: str) -> None:
        stripped = re.sub(r"(--[^\n]*|/\*.*?\*/)", " ", sql, flags=re.DOTALL).lower()
        if ";" in stripped.rstrip(";").strip():
            raise PolicyViolationError(
                "multiple statements are not permitted in one query",
            )
        tokens = set(re.findall(r"[a-z]+", stripped))
        offending = sorted(tokens & _WRITE_KEYWORDS)
        if offending:
            raise PolicyViolationError(
                "query contains mutating SQL keywords",
                keywords=offending,
            )
        if not stripped.lstrip().startswith(("select", "with", "explain", "show")):
            raise PolicyViolationError(
                "only SELECT, WITH, EXPLAIN and SHOW queries are permitted",
            )


# ---------------------------------------------------------------------------
# send_email (high risk, approval-gated) -- reference implementation only
# ---------------------------------------------------------------------------


class SendEmailTool(Tool):
    """Send an email. HIGH risk, approval-gated, and does not actually send.

    This is the human-in-the-loop demonstration tool. It deliberately does not
    integrate a mail provider: the interesting behaviour is the approval gate,
    and shipping a tool that can really email strangers from a reference
    implementation would be irresponsible. Calls are recorded to an outbox file
    so the demo can show what *would* have been sent.
    """

    spec = ToolSpec(
        name="send_email",
        description="Send an email to a recipient. Requires human approval.",
        input_schema=object_schema(
            {
                "to": {"type": "string", "minLength": 3, "maxLength": 320},
                "subject": {"type": "string", "minLength": 1, "maxLength": 300},
                "body": {"type": "string", "minLength": 1, "maxLength": 50_000},
            },
            required=["to", "subject", "body"],
        ),
        risk=RiskLevel.HIGH,
        requires_approval=True,
        idempotent=False,
        retry_policy=NO_RETRY_POLICY,
        timeout_seconds=15.0,
        tags=frozenset({"communication", "external", "write"}),
    )

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        recipient = str(arguments["to"])
        if "@" not in recipient:
            raise InputValidationError("recipient is not an email address", to=recipient)

        outbox = context.sandbox_root.resolve() / "outbox"
        message_id = hashlib.sha256(
            f"{context.execution_id}:{recipient}:{arguments['subject']}".encode()
        ).hexdigest()[:16]

        def _record() -> None:
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / f"{message_id}.json").write_text(
                json.dumps(
                    {
                        "message_id": message_id,
                        "execution_id": context.execution_id,
                        "to": recipient,
                        "subject": arguments["subject"],
                        "body": arguments["body"],
                        "delivered": False,
                        "note": "reference implementation: recorded, not sent",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        await asyncio.to_thread(_record)
        return {
            "message_id": message_id,
            "to": recipient,
            "delivered": False,
            "note": "recorded to outbox; this reference implementation does not send mail",
        }


# ---------------------------------------------------------------------------
# exec_shell (critical, disabled by default)
# ---------------------------------------------------------------------------


class ExecShellTool(Tool):
    """Execute a shell command. CRITICAL risk; not registered unless enabled.

    Present so the risk/approval machinery has a genuine worst case to govern.
    It is never registered unless ``ORCH_ENABLE_SHELL_TOOL=true``, its ``ToolSpec``
    is forced by validation to be approval-gated and not enabled by default, and
    the security documentation states plainly that enabling it grants arbitrary
    code execution to whatever can reach the API.
    """

    spec = ToolSpec(
        name="exec_shell",
        description="Execute a shell command and return its output. Requires human approval.",
        input_schema=object_schema(
            {
                "command": {"type": "string", "minLength": 1, "maxLength": 4_000},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
            },
            required=["command"],
        ),
        risk=RiskLevel.CRITICAL,
        requires_approval=True,
        enabled_by_default=False,
        idempotent=False,
        retry_policy=NO_RETRY_POLICY,
        timeout_seconds=120.0,
        tags=frozenset({"system", "dangerous"}),
    )

    MAX_OUTPUT_BYTES: ClassVar[int] = 100_000

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        command = str(arguments["command"])
        timeout = min(float(arguments.get("timeout_seconds", 30.0)), context.deadline_seconds)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(context.sandbox_root.resolve()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return {
            "command": command,
            "exit_code": process.returncode,
            "stdout": stdout[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        }
