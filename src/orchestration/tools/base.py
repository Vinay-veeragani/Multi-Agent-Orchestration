"""Tool interface and execution context.

A tool is a :class:`ToolSpec` (declarative data, in the domain layer) paired with
an async callable. This module defines that pairing and the guarantees the
runtime provides around every call:

1. **Arguments are validated against the tool's JSON Schema before it runs.**
   Never by asking the model nicely -- by rejecting the call.
2. **A tool receives a narrow context, not the engine.** It gets an execution id,
   a sandbox root, and a deadline. It cannot reach the registry, the database, or
   other executions.
3. **Failures are exceptions from the taxonomy**, so the retry layer can classify
   them without string matching.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from orchestration.domain.base import JsonDict
from orchestration.domain.tool import ToolSpec
from orchestration.errors import (
    ConfigurationError,
    EngineTimeoutError,
    InputValidationError,
)


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to know about its caller.

    Deliberately minimal. A tool that could reach the registry or the database
    would be able to escalate its own permissions, so the context carries
    identifiers and limits rather than live services.

    Attributes:
        execution_id: For correlating artifacts and logs.
        agent_id: Which agent requested the call, or ``None`` for a tool node.
        node_id: The workflow node in scope.
        sandbox_root: Filesystem operations are confined below this directory.
        deadline_seconds: Remaining time; a tool should not exceed it.
        attempt: 1-based attempt number, for logging and idempotency keys.
        constraints: Argument constraints from the agent's permission entry,
            already applied by the policy engine -- passed through so a tool can
            make a stricter local decision if it wants to.
    """

    execution_id: str
    agent_id: str | None = None
    node_id: str | None = None
    sandbox_root: Path = field(default_factory=lambda: Path("./.artifacts"))
    deadline_seconds: float = 30.0
    attempt: int = 1
    constraints: JsonDict = field(default_factory=dict)

    def resolve_in_sandbox(self, candidate: str) -> Path:
        """Resolve ``candidate`` inside the sandbox, rejecting escapes.

        Uses ``resolve()`` on both sides before comparing, so ``..`` traversal,
        symlinks, and absolute paths are all normalised away before the check.
        Returning a path only when it is genuinely inside the root means callers
        cannot forget to validate.

        Raises:
            InputValidationError: If the path resolves outside the sandbox.
        """
        root = self.sandbox_root.resolve()
        target = (
            (root / candidate).resolve()
            if not Path(candidate).is_absolute()
            else Path(candidate).resolve()
        )
        if not target.is_relative_to(root):
            raise InputValidationError(
                "path escapes the tool sandbox",
                path=candidate,
                sandbox_root=str(root),
            )
        return target


class Tool(abc.ABC):
    """Base class for an executable tool.

    Subclasses provide a :attr:`spec` and implement :meth:`run`. The public entry
    point is :meth:`invoke`, which is what the runtime calls: it validates
    arguments and enforces the timeout, then delegates to :meth:`run`.

    Making ``invoke`` non-overridable in practice (subclasses implement ``run``)
    is what guarantees no tool can skip argument validation.
    """

    #: Set by the subclass. The declarative half of the tool.
    spec: ToolSpec

    def __init__(self) -> None:
        if not hasattr(self, "spec"):
            raise ConfigurationError(
                f"{type(self).__name__} must define a `spec` class attribute",
            )
        # Compiling the validator once per tool rather than per call: schema
        # compilation dominates validation cost for small argument objects.
        self._validator = Draft202012Validator(self.spec.input_schema)

    @property
    def name(self) -> str:
        return self.spec.name

    @abc.abstractmethod
    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        """Perform the tool's work.

        Args:
            arguments: Already validated against :attr:`ToolSpec.input_schema`.
            context: Caller identity and limits.

        Returns:
            A JSON-serialisable result object.

        Raises:
            OrchestrationError: Subclasses should raise from the taxonomy so the
                retry layer can classify the failure.
        """

    def validate_arguments(self, arguments: JsonDict) -> None:
        """Validate ``arguments`` against the declared input schema.

        Collects *all* schema errors rather than raising on the first, because an
        agent given the complete list can fix its call in one further attempt
        instead of discovering problems one at a time.

        Raises:
            InputValidationError: If the arguments do not conform.
        """
        errors = sorted(self._validator.iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            problems = [self._describe_schema_error(e) for e in errors]
            raise InputValidationError(
                f"invalid arguments for tool {self.spec.name!r}",
                tool=self.spec.name,
                problems=problems,
            )

    @staticmethod
    def _describe_schema_error(error: JsonSchemaValidationError) -> str:
        location = ".".join(str(p) for p in error.path) or "<root>"
        return f"{location}: {error.message}"

    async def invoke(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        """Validate, then run under a timeout.

        The timeout is the smaller of the tool's declared ceiling and the
        caller's remaining deadline: a tool must not be able to outlive the
        execution's duration budget by declaring a generous timeout of its own.
        """
        self.validate_arguments(arguments)
        timeout = min(self.spec.timeout_seconds, context.deadline_seconds)
        try:
            async with asyncio.timeout(timeout):
                return await self.run(arguments, context)
        except TimeoutError as exc:
            raise EngineTimeoutError(
                f"tool {self.spec.name!r} exceeded its {timeout:.1f}s timeout",
                tool=self.spec.name,
                timeout_seconds=timeout,
            ) from exc

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.spec.name!r} risk={self.spec.risk.value}>"


class FunctionTool(Tool):
    """Adapter turning a plain async function into a :class:`Tool`.

    Exists so that registering a tool at runtime -- from a plugin, a test, or a
    demo -- does not require declaring a class.
    """

    def __init__(
        self,
        spec: ToolSpec,
        handler: Callable[[JsonDict, ToolContext], Awaitable[JsonDict]],
    ) -> None:
        self.spec = spec
        self._handler = handler
        super().__init__()

    async def run(self, arguments: JsonDict, context: ToolContext) -> JsonDict:
        return await self._handler(arguments, context)


def tool_from_function(
    *,
    name: str,
    description: str,
    input_schema: JsonDict,
    handler: Callable[[JsonDict, ToolContext], Awaitable[JsonDict]],
    **spec_kwargs: Any,
) -> FunctionTool:
    """Build a :class:`FunctionTool` from a handler and schema."""
    spec = ToolSpec(name=name, description=description, input_schema=input_schema, **spec_kwargs)
    return FunctionTool(spec, handler)


#: Schema fragment reused by tools that take no arguments.
EMPTY_SCHEMA: Final[JsonDict] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def object_schema(
    properties: dict[str, JsonDict],
    *,
    required: list[str] | None = None,
    additional: bool = False,
) -> JsonDict:
    """Build a strict object schema.

    ``additionalProperties`` defaults to ``False``: an unexpected argument from a
    model is a signal that it misunderstood the tool, and silently dropping it
    would hide that.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": additional,
    }
