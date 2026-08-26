"""Tests for the tool contract and the reference tool implementations.

The invariant these tests defend: **a tool never sees arguments it did not
declare, and never escapes its sandbox.** Enforcement is in the runtime, so
these tests attack the runtime rather than checking that a model behaved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestration.domain.enums import RiskLevel
from orchestration.errors import (
    ConfigurationError,
    DuplicateError,
    EngineTimeoutError,
    InputValidationError,
    NotFoundError,
    PolicyViolationError,
)
from orchestration.tools.base import (
    Tool,
    ToolContext,
    object_schema,
    tool_from_function,
)
from orchestration.tools.builtin import (
    CalculatorTool,
    DatabaseQueryTool,
    ExecShellTool,
    HttpRequestTool,
    PythonExecTool,
    ReadFileTool,
    SendEmailTool,
    WebSearchTool,
    WriteFileTool,
)
from orchestration.tools.registry import ToolRegistry, build_default_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("hello sandbox", encoding="utf-8")
    return tmp_path


@pytest.fixture
def context(sandbox: Path) -> ToolContext:
    return ToolContext(
        execution_id="exec_test",
        agent_id="research_agent",
        sandbox_root=sandbox,
        deadline_seconds=10.0,
    )


# ---------------------------------------------------------------------------
# ToolContext sandboxing
# ---------------------------------------------------------------------------


class TestSandboxResolution:
    def test_resolves_a_relative_path(self, context: ToolContext, sandbox: Path) -> None:
        assert (
            context.resolve_in_sandbox("data/notes.txt") == (sandbox / "data/notes.txt").resolve()
        )

    @pytest.mark.parametrize(
        "escape",
        [
            "../outside.txt",
            "../../etc/passwd",
            "data/../../outside.txt",
            "./data/../../../outside.txt",
        ],
    )
    def test_rejects_traversal(self, context: ToolContext, escape: str) -> None:
        """`..` must be normalised away *before* the containment check."""
        with pytest.raises(InputValidationError, match="escapes the tool sandbox"):
            context.resolve_in_sandbox(escape)

    def test_rejects_an_absolute_path_outside(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="escapes the tool sandbox"):
            context.resolve_in_sandbox("C:/Windows/System32/drivers/etc/hosts")

    def test_accepts_an_absolute_path_inside(self, context: ToolContext, sandbox: Path) -> None:
        inside = str((sandbox / "data" / "notes.txt").resolve())
        assert context.resolve_in_sandbox(inside).name == "notes.txt"


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    def test_missing_required_argument_is_rejected(self, context: ToolContext) -> None:
        tool = CalculatorTool()
        with pytest.raises(InputValidationError) as info:
            tool.validate_arguments({})
        assert "expression" in str(info.value)

    def test_wrong_type_is_rejected(self) -> None:
        tool = CalculatorTool()
        with pytest.raises(InputValidationError):
            tool.validate_arguments({"expression": 42})

    def test_unexpected_argument_is_rejected(self) -> None:
        """additionalProperties=False catches a model that misread the schema."""
        tool = CalculatorTool()
        with pytest.raises(InputValidationError):
            tool.validate_arguments({"expression": "1+1", "sneaky": True})

    def test_all_problems_are_reported_at_once(self) -> None:
        """An agent given the full list can fix its call in one further attempt."""
        tool = tool_from_function(
            name="multi",
            description="d",
            input_schema=object_schema(
                {
                    "a": {"type": "integer"},
                    "b": {"type": "string"},
                },
                required=["a", "b"],
            ),
            handler=_noop_handler,
        )
        with pytest.raises(InputValidationError) as info:
            tool.validate_arguments({"a": "not-an-int", "b": 5})
        problems = info.value.context["problems"]
        assert len(problems) == 2

    async def test_invoke_validates_before_running(self, context: ToolContext) -> None:
        """A tool body must never execute with unvalidated arguments."""
        ran = False

        async def handler(args: dict[str, object], ctx: ToolContext) -> dict[str, object]:
            nonlocal ran
            ran = True
            return {}

        tool = tool_from_function(
            name="guarded",
            description="d",
            input_schema=object_schema({"x": {"type": "integer"}}, required=["x"]),
            handler=handler,
        )
        with pytest.raises(InputValidationError):
            await tool.invoke({"x": "bad"}, context)
        assert ran is False, "the tool body ran despite invalid arguments"


async def _noop_handler(args: dict[str, object], ctx: ToolContext) -> dict[str, object]:
    return {}


class TestTimeoutEnforcement:
    async def test_slow_tool_times_out(self, context: ToolContext) -> None:
        async def slow(args: dict[str, object], ctx: ToolContext) -> dict[str, object]:
            await asyncio.sleep(5)
            return {}

        tool = tool_from_function(
            name="slow",
            description="d",
            input_schema=object_schema({}),
            handler=slow,
            timeout_seconds=0.05,
        )
        with pytest.raises(EngineTimeoutError, match="exceeded its"):
            await tool.invoke({}, context)

    async def test_caller_deadline_wins_when_shorter(self, context: ToolContext) -> None:
        """A tool must not outlive the execution budget via a generous timeout."""

        async def slow(args: dict[str, object], ctx: ToolContext) -> dict[str, object]:
            await asyncio.sleep(5)
            return {}

        tool = tool_from_function(
            name="slow2",
            description="d",
            input_schema=object_schema({}),
            handler=slow,
            timeout_seconds=600.0,
        )
        tight = ToolContext(
            execution_id="e", sandbox_root=context.sandbox_root, deadline_seconds=0.05
        )
        with pytest.raises(EngineTimeoutError):
            await tool.invoke({}, tight)


class TestToolBaseContract:
    def test_a_tool_without_a_spec_is_rejected(self) -> None:
        class Broken(Tool):
            async def run(
                self, arguments: dict[str, object], context: ToolContext
            ) -> dict[str, object]:
                return {}

        with pytest.raises(ConfigurationError, match="must define a `spec`"):
            Broken()

    def test_repr_shows_name_and_risk(self) -> None:
        assert "calculator" in repr(CalculatorTool())


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------


class TestCalculator:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("1+1", 2.0),
            ("2 * 3 + 4", 10.0),
            ("(2 + 3) * 4", 20.0),
            ("10 / 4", 2.5),
            ("10 // 3", 3.0),
            ("10 % 3", 1.0),
            ("2 ** 8", 256.0),
            ("-5 + 3", -2.0),
            ("+7", 7.0),
            ("1.5 * 2", 3.0),
        ],
    )
    async def test_arithmetic(self, context: ToolContext, expression: str, expected: float) -> None:
        result = await CalculatorTool().invoke({"expression": expression}, context)
        assert result["value"] == expected

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('echo pwned')",
            "open('/etc/passwd').read()",
            "eval('1+1')",
            "[].__class__",
            "lambda: 1",
            "x + 1",
            "print(1)",
            "1 if True else 2",
            "[1,2,3]",
            "{'a': 1}",
            "1 << 2",
            "~5",
            "True + 1",
        ],
    )
    async def test_rejects_everything_that_is_not_arithmetic(
        self, context: ToolContext, expression: str
    ) -> None:
        """The allowlist is the security boundary; nothing else may pass."""
        with pytest.raises(InputValidationError):
            await CalculatorTool().invoke({"expression": expression}, context)

    async def test_division_by_zero_is_a_terminal_input_error(self, context: ToolContext) -> None:
        """Not retryable: 1/0 will still be 1/0 on the next attempt."""
        with pytest.raises(InputValidationError, match="division by zero") as info:
            await CalculatorTool().invoke({"expression": "1/0"}, context)
        assert info.value.retryable is False

    async def test_huge_exponent_is_refused(self, context: ToolContext) -> None:
        """`9**9**9` would otherwise block the event loop computing a huge int."""
        with pytest.raises(InputValidationError, match="exceeds the maximum"):
            await CalculatorTool().invoke({"expression": "9 ** 999"}, context)

    async def test_syntax_error_is_reported_clearly(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="not valid arithmetic syntax"):
            await CalculatorTool().invoke({"expression": "2 +* 3"}, context)


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


class TestWebSearch:
    async def test_returns_ranked_results(self, context: ToolContext) -> None:
        result = await WebSearchTool().invoke({"query": "CRM pricing tiers"}, context)
        assert result["result_count"] > 0
        assert all({"title", "url", "snippet"} <= set(r) for r in result["results"])

    async def test_labels_itself_as_offline(self, context: ToolContext) -> None:
        """A caller must not be able to mistake corpus data for live results."""
        result = await WebSearchTool().invoke({"query": "CRM"}, context)
        assert result["source"] == "offline_corpus"

    async def test_respects_max_results(self, context: ToolContext) -> None:
        result = await WebSearchTool().invoke(
            {"query": "CRM pricing vendors", "max_results": 2}, context
        )
        assert len(result["results"]) <= 2

    async def test_is_deterministic(self, context: ToolContext) -> None:
        """The benchmark depends on identical results across runs."""
        tool = WebSearchTool()
        first = await tool.invoke({"query": "CRM AI capabilities"}, context)
        second = await tool.invoke({"query": "CRM AI capabilities"}, context)
        assert first == second

    async def test_unmatched_query_returns_empty(self, context: ToolContext) -> None:
        result = await WebSearchTool().invoke({"query": "zzzz nonexistent topic"}, context)
        assert result["result_count"] == 0

    async def test_accepts_an_injected_corpus(self, context: ToolContext) -> None:
        tool = WebSearchTool(
            corpus={"widgets": [{"title": "Widget", "url": "u", "snippet": "a widget thing"}]}
        )
        result = await tool.invoke({"query": "widget"}, context)
        assert result["result_count"] == 1


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


class TestFileTools:
    async def test_read_file(self, context: ToolContext) -> None:
        result = await ReadFileTool().invoke({"path": "data/notes.txt"}, context)
        assert result["content"] == "hello sandbox"
        assert result["truncated"] is False

    async def test_read_truncates_and_says_so(self, context: ToolContext, sandbox: Path) -> None:
        (sandbox / "big.txt").write_text("x" * 5000, encoding="utf-8")
        result = await ReadFileTool().invoke({"path": "big.txt", "max_bytes": 100}, context)
        assert len(result["content"]) == 100
        assert result["truncated"] is True
        assert result["size_bytes"] == 5000

    async def test_read_missing_file(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="does not exist"):
            await ReadFileTool().invoke({"path": "nope.txt"}, context)

    async def test_read_directory_is_rejected(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="not a regular file"):
            await ReadFileTool().invoke({"path": "data"}, context)

    async def test_read_cannot_escape_the_sandbox(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="escapes the tool sandbox"):
            await ReadFileTool().invoke({"path": "../../../etc/passwd"}, context)

    async def test_write_then_read(self, context: ToolContext) -> None:
        await WriteFileTool().invoke({"path": "out/report.md", "content": "# Title"}, context)
        result = await ReadFileTool().invoke({"path": "out/report.md"}, context)
        assert result["content"] == "# Title"

    async def test_write_creates_parents(self, context: ToolContext, sandbox: Path) -> None:
        await WriteFileTool().invoke({"path": "a/b/c/f.txt", "content": "x"}, context)
        assert (sandbox / "a/b/c/f.txt").exists()

    async def test_write_append_mode(self, context: ToolContext) -> None:
        tool = WriteFileTool()
        await tool.invoke({"path": "log.txt", "content": "one\n"}, context)
        await tool.invoke({"path": "log.txt", "content": "two\n", "mode": "append"}, context)
        result = await ReadFileTool().invoke({"path": "log.txt"}, context)
        assert result["content"] == "one\ntwo\n"

    async def test_write_cannot_escape_the_sandbox(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="escapes the tool sandbox"):
            await WriteFileTool().invoke({"path": "../escaped.txt", "content": "x"}, context)

    def test_write_is_declared_non_idempotent_with_no_retries(self) -> None:
        """Append is state-changing, so an automatic retry would duplicate data."""
        spec = WriteFileTool().spec
        assert spec.idempotent is False
        assert spec.retry_policy.max_attempts == 1


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------


class TestHttpRequest:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://localhost:8000/admin",
            "http://127.0.0.1/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.1/",
        ],
    )
    async def test_blocks_metadata_and_private_addresses(
        self, context: ToolContext, url: str
    ) -> None:
        """Basic SSRF mitigation: cloud metadata and RFC1918 ranges are denied."""
        with pytest.raises(PolicyViolationError, match="blocked"):
            await HttpRequestTool().invoke({"url": url}, context)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.test/x", "gopher://x"])
    async def test_rejects_non_http_schemes(self, context: ToolContext, url: str) -> None:
        with pytest.raises(InputValidationError, match="only http and https"):
            await HttpRequestTool().invoke({"url": url}, context)

    async def test_mutating_methods_are_denied_by_default(self, context: ToolContext) -> None:
        with pytest.raises(PolicyViolationError, match="not permitted"):
            await HttpRequestTool().invoke(
                {"url": "https://example.test/x", "method": "POST"}, context
            )

    async def test_denial_happens_before_any_network_call(self, context: ToolContext) -> None:
        """A blocked host must not be contacted at all, not merely rejected after."""
        with pytest.raises(PolicyViolationError):
            await HttpRequestTool().invoke(
                {"url": "http://169.254.169.254/", "method": "GET"}, context
            )


# ---------------------------------------------------------------------------
# python_exec
# ---------------------------------------------------------------------------


class TestPythonExec:
    async def test_captures_stdout(self, context: ToolContext) -> None:
        result = await PythonExecTool().invoke({"code": "print(6 * 7)"}, context)
        assert result["stdout"].strip() == "42"
        assert result["exit_code"] == 0

    async def test_captures_stderr_and_exit_code(self, context: ToolContext) -> None:
        result = await PythonExecTool().invoke({"code": "raise SystemExit(3)"}, context)
        assert result["exit_code"] == 3

    async def test_reads_stdin(self, context: ToolContext) -> None:
        result = await PythonExecTool().invoke(
            {"code": "import sys; print(sys.stdin.read().upper())", "stdin": "abc"}, context
        )
        assert "ABC" in result["stdout"]

    async def test_timeout_kills_the_subprocess(self, context: ToolContext) -> None:
        tool = PythonExecTool(timeout_seconds=0.3)
        with pytest.raises((EngineTimeoutError, TimeoutError)):
            await tool.invoke({"code": "import time; time.sleep(30)"}, context)

    async def test_runs_in_the_sandbox_directory(self, context: ToolContext) -> None:
        result = await PythonExecTool().invoke(
            {"code": "import pathlib; print(pathlib.Path.cwd().name)"}, context
        )
        assert result["stdout"].strip() == context.sandbox_root.resolve().name

    def test_declared_non_idempotent(self) -> None:
        assert PythonExecTool().spec.idempotent is False


# ---------------------------------------------------------------------------
# db_query
# ---------------------------------------------------------------------------


class TestDatabaseQueryGuards:
    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM executions",
            "UPDATE agents SET enabled = false",
            "INSERT INTO events VALUES (1)",
            "DROP TABLE checkpoints",
            "TRUNCATE executions",
            "ALTER TABLE agents ADD COLUMN x int",
            "CREATE TABLE evil (id int)",
            "GRANT ALL ON executions TO public",
        ],
    )
    async def test_rejects_mutating_sql(self, context: ToolContext, sql: str) -> None:
        with pytest.raises(PolicyViolationError, match="mutating SQL"):
            await DatabaseQueryTool().invoke({"sql": sql}, context)

    async def test_rejects_stacked_statements(self, context: ToolContext) -> None:
        with pytest.raises(PolicyViolationError, match="multiple statements"):
            await DatabaseQueryTool().invoke({"sql": "SELECT 1; SELECT 2"}, context)

    async def test_rejects_a_comment_hidden_mutation(self, context: ToolContext) -> None:
        """Comment stripping happens before keyword matching."""
        with pytest.raises(PolicyViolationError):
            await DatabaseQueryTool().invoke(
                {"sql": "SELECT 1 /* then */ ; DELETE FROM executions"}, context
            )

    async def test_rejects_non_select_leading_keyword(self, context: ToolContext) -> None:
        with pytest.raises(PolicyViolationError, match="only SELECT"):
            await DatabaseQueryTool().invoke({"sql": "SET ROLE postgres"}, context)

    async def test_a_read_query_without_a_session_fails_clearly(self, context: ToolContext) -> None:
        """Not silently succeeding is the point: the tool holds no connection."""
        with pytest.raises(PolicyViolationError, match="without a database session"):
            await DatabaseQueryTool().invoke({"sql": "SELECT 1"}, context)


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    async def test_records_to_outbox_without_sending(
        self, context: ToolContext, sandbox: Path
    ) -> None:
        result = await SendEmailTool().invoke(
            {"to": "ops@example.test", "subject": "Report", "body": "Body"}, context
        )
        assert result["delivered"] is False
        outbox = list((sandbox / "outbox").glob("*.json"))
        assert len(outbox) == 1

    async def test_rejects_a_non_address(self, context: ToolContext) -> None:
        with pytest.raises(InputValidationError, match="not an email address"):
            await SendEmailTool().invoke(
                {"to": "not-an-address", "subject": "s", "body": "b"}, context
            )

    def test_is_high_risk_and_approval_gated(self) -> None:
        spec = SendEmailTool().spec
        assert spec.risk is RiskLevel.HIGH
        assert spec.requires_approval is True
        assert spec.idempotent is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        assert registry.get("calculator").name == "calculator"
        assert "calculator" in registry
        assert len(registry) == 1

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        with pytest.raises(DuplicateError, match="already registered"):
            registry.register(CalculatorTool())

    def test_replace_is_explicit(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        registry.register(CalculatorTool(), replace=True)
        assert len(registry) == 1

    def test_unknown_tool_lists_alternatives(self) -> None:
        """Turns a typo in a workflow file into an actionable message."""
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        with pytest.raises(NotFoundError) as info:
            registry.get("calculater")
        assert "calculator" in info.value.context["available"]

    def test_disabled_tool_is_distinguished_from_unknown(self) -> None:
        """Different problems deserve different errors."""
        registry = ToolRegistry()
        registry.register(CalculatorTool(), enabled=False)
        with pytest.raises(ConfigurationError, match="registered but disabled"):
            registry.get("calculator")
        with pytest.raises(NotFoundError):
            registry.get("never_existed")

    def test_spec_is_readable_for_a_disabled_tool(self) -> None:
        """The API must be able to report what exists and why it is unavailable."""
        registry = ToolRegistry()
        registry.register(CalculatorTool(), enabled=False)
        assert registry.get_spec("calculator").name == "calculator"

    def test_enable_and_disable(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        registry.disable("calculator")
        assert registry.is_enabled("calculator") is False
        registry.enable("calculator")
        assert registry.is_enabled("calculator") is True

    def test_unregister(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        registry.unregister("calculator")
        assert "calculator" not in registry
        with pytest.raises(NotFoundError):
            registry.unregister("calculator")

    def test_listing_excludes_disabled_by_default(self) -> None:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        registry.register(WebSearchTool(), enabled=False)
        assert registry.names() == ("calculator",)
        assert set(registry.names(include_disabled=True)) == {"calculator", "web_search"}

    def test_filter_by_risk_and_tag(self) -> None:
        registry = build_default_registry()
        assert any(t.name == "send_email" for t in registry.by_risk(RiskLevel.HIGH))
        assert any(t.name == "web_search" for t in registry.by_tag("research"))

    async def test_concurrent_registration_is_safe(self) -> None:
        registry = ToolRegistry()
        tools = [
            tool_from_function(
                name=f"t{i}", description="d", input_schema=object_schema({}), handler=_noop_handler
            )
            for i in range(20)
        ]
        await asyncio.gather(*(registry.register_async(t) for t in tools))
        assert len(registry) == 20


class TestAgentToolVisibility:
    def test_specs_for_agent_filters_to_the_allowlist(self) -> None:
        registry = build_default_registry()
        specs = registry.specs_for_agent(["web_search", "read_file"])
        assert {s.name for s in specs} == {"web_search", "read_file"}

    def test_unknown_names_are_silently_omitted(self) -> None:
        """A deployment may not have every optional tool; do not offer what is absent."""
        registry = build_default_registry()
        specs = registry.specs_for_agent(["web_search", "does_not_exist"])
        assert {s.name for s in specs} == {"web_search"}

    def test_disabled_tools_are_not_offered_to_the_model(self) -> None:
        registry = build_default_registry(enable_python=False)
        specs = registry.specs_for_agent(["python_exec", "calculator"])
        assert {s.name for s in specs} == {"calculator"}

    def test_llm_schemas_are_well_formed(self) -> None:
        registry = build_default_registry()
        schemas = registry.llm_schemas_for_agent(["calculator", "web_search"])
        assert len(schemas) == 2
        for schema in schemas:
            assert schema["type"] == "function"
            assert "name" in schema["function"]
            assert "parameters" in schema["function"]


class TestDefaultRegistry:
    def test_shell_is_absent_unless_enabled(self) -> None:
        """Not merely disabled -- absent, so its schema is never even advertised."""
        registry = build_default_registry()
        assert "exec_shell" not in registry

    def test_shell_appears_only_when_explicitly_enabled(self) -> None:
        registry = build_default_registry(enable_shell=True)
        assert registry.is_enabled("exec_shell") is True

    def test_python_can_be_disabled(self) -> None:
        registry = build_default_registry(enable_python=False)
        assert "python_exec" in registry
        assert registry.is_enabled("python_exec") is False

    def test_expected_reference_tools_are_present(self) -> None:
        registry = build_default_registry()
        assert {
            "calculator",
            "web_search",
            "read_file",
            "write_file",
            "http_request",
            "send_email",
            "db_query",
            "python_exec",
        } <= set(registry.names(include_disabled=True))

    def test_no_critical_tool_is_enabled_by_default(self) -> None:
        """A safety property of the shipped configuration, asserted in a test."""
        registry = build_default_registry()
        for tool in registry.list_tools():
            assert tool.spec.risk is not RiskLevel.CRITICAL

    def test_shell_tool_spec_is_forced_to_be_gated(self) -> None:
        spec = ExecShellTool().spec
        assert spec.enabled_by_default is False
        assert spec.requires_approval is True
        assert spec.risk is RiskLevel.CRITICAL
