"""Tool registry.

Holds the live tool instances keyed by name and answers the two questions the
runtime asks: "does this tool exist and is it enabled", and "what may this agent
see". Registration is dynamic -- tools can be added at runtime -- but the registry
is deliberately not a service locator: it hands back tools, never connections or
credentials.

Enabled/disabled is tracked separately from registration so that a dangerous tool
can be *known* (and therefore reportable, and refusable with a clear reason)
without being callable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator

from orchestration.domain.base import JsonDict
from orchestration.domain.enums import RiskLevel
from orchestration.domain.tool import ToolSpec
from orchestration.errors import ConfigurationError, DuplicateError, NotFoundError
from orchestration.tools.base import Tool


class ToolRegistry:
    """A mutable collection of tools, safe for concurrent async access.

    The lock protects the mapping during mutation. Reads are lock-free because
    dict lookup is atomic under the GIL and the values are immutable specs plus
    tool objects that carry no per-call state.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._lock = asyncio.Lock()

    # -- registration ------------------------------------------------------

    def register(self, tool: Tool, *, replace: bool = False, enabled: bool | None = None) -> Tool:
        """Add ``tool`` to the registry.

        Args:
            tool: The tool instance.
            replace: Permit overwriting an existing registration.
            enabled: Force enabled state. Defaults to the spec's
                ``enabled_by_default``, which is how dangerous tools stay off
                unless an operator explicitly turns them on.

        Raises:
            DuplicateError: If the name is taken and ``replace`` is false.
        """
        name = tool.spec.name
        if name in self._tools and not replace:
            raise DuplicateError(
                f"tool {name!r} is already registered",
                tool=name,
                hint="pass replace=True to overwrite",
            )
        self._tools[name] = tool
        should_enable = tool.spec.enabled_by_default if enabled is None else enabled
        if should_enable:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        return tool

    async def register_async(self, tool: Tool, *, replace: bool = False) -> Tool:
        """Register under the lock, for concurrent registration paths (the API)."""
        async with self._lock:
            return self.register(tool, replace=replace)

    def unregister(self, name: str) -> None:
        """Remove a tool.

        Raises:
            NotFoundError: If no such tool is registered.
        """
        if name not in self._tools:
            raise NotFoundError(f"tool {name!r} is not registered", tool=name)
        del self._tools[name]
        self._disabled.discard(name)

    def register_all(self, tools: Iterable[Tool], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    # -- lookup ------------------------------------------------------------

    def get(self, name: str) -> Tool:
        """Fetch an enabled tool.

        Raises:
            NotFoundError: If the tool is unknown.
            ConfigurationError: If it is registered but disabled. Distinguished
                from "unknown" on purpose: an agent asking for a disabled tool is
                a configuration problem worth surfacing differently from a typo.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(
                f"tool {name!r} is not registered",
                tool=name,
                available=sorted(self._tools),
            )
        if name in self._disabled:
            raise ConfigurationError(
                f"tool {name!r} is registered but disabled",
                tool=name,
                risk=tool.spec.risk.value,
                hint="enable it explicitly in configuration if you intend to allow it",
            )
        return tool

    def get_spec(self, name: str) -> ToolSpec:
        """Fetch a spec, even for a disabled tool.

        Specs of disabled tools remain readable so the API can report what exists
        and why it is unavailable.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(f"tool {name!r} is not registered", tool=name)
        return tool.spec

    def has(self, name: str) -> bool:
        return name in self._tools

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        if name not in self._tools:
            raise NotFoundError(f"tool {name!r} is not registered", tool=name)
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        if name not in self._tools:
            raise NotFoundError(f"tool {name!r} is not registered", tool=name)
        self._disabled.add(name)

    # -- listing -----------------------------------------------------------

    def list_tools(self, *, include_disabled: bool = False) -> tuple[Tool, ...]:
        return tuple(
            tool
            for name, tool in sorted(self._tools.items())
            if include_disabled or name not in self._disabled
        )

    def list_specs(self, *, include_disabled: bool = False) -> tuple[ToolSpec, ...]:
        return tuple(t.spec for t in self.list_tools(include_disabled=include_disabled))

    def names(self, *, include_disabled: bool = False) -> tuple[str, ...]:
        return tuple(t.spec.name for t in self.list_tools(include_disabled=include_disabled))

    def by_risk(self, risk: RiskLevel) -> tuple[Tool, ...]:
        return tuple(t for t in self.list_tools(include_disabled=True) if t.spec.risk is risk)

    def by_tag(self, tag: str) -> tuple[Tool, ...]:
        return tuple(t for t in self.list_tools() if tag in t.spec.tags)

    def specs_for_agent(self, allowed: Iterable[str]) -> tuple[ToolSpec, ...]:
        """Specs for the subset of ``allowed`` that is registered and enabled.

        Silently omits unknown or disabled names rather than raising: an agent
        definition may legitimately list a tool that this deployment has not
        enabled, and the correct behaviour is to not offer it to the model.
        Attempting to *call* it still fails loudly via :meth:`get`.
        """
        return tuple(
            self._tools[name].spec
            for name in sorted(set(allowed))
            if name in self._tools and name not in self._disabled
        )

    def llm_schemas_for_agent(self, allowed: Iterable[str]) -> tuple[JsonDict, ...]:
        """Function declarations for the tools an agent may actually use."""
        return tuple(spec.to_llm_schema() for spec in self.specs_for_agent(allowed))

    # -- dunder ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self.list_tools())

    def __repr__(self) -> str:
        return (
            f"<ToolRegistry tools={len(self._tools)} "
            f"enabled={len(self._tools) - len(self._disabled)}>"
        )


def build_default_registry(
    *,
    enable_shell: bool = False,
    enable_python: bool = True,
    session_factory: object | None = None,
    allow_mutating_http: bool = False,
) -> ToolRegistry:
    """Construct a registry with the reference tools.

    Dangerous capabilities are opt-in parameters rather than ambient
    configuration reads, so a caller (a test, a demo, the API) has to state what
    it wants and the decision is visible at the call site.
    """
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

    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(WebSearchTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(HttpRequestTool(allow_mutating_methods=allow_mutating_http))
    registry.register(SendEmailTool())
    registry.register(DatabaseQueryTool(session_factory=session_factory))

    # python_exec is registered but respects the flag: the data-analysis demo
    # needs it, and a deployment that does not want code execution can drop it.
    registry.register(PythonExecTool(), enabled=enable_python)

    # exec_shell is only registered at all when explicitly enabled. Registering
    # it disabled would still expose its schema through the API, and there is no
    # reason to advertise arbitrary command execution that cannot be used.
    if enable_shell:
        registry.register(ExecShellTool(), enabled=True)

    return registry
