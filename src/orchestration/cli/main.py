"""``orchestrator`` -- the CLI entry point (see ``pyproject.toml``'s ``[project.scripts]``).

Every command that talks to a running engine goes through :class:`ApiClient`
against ``--api-url`` (default ``http://127.0.0.1:8000``, or ``$ORCH_API_URL``).
``benchmark`` is the one command that does not: it runs the evaluation
harness directly against the configured database and Redis, exactly like
``benchmarks/run_benchmark.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, TypeVar

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from orchestration.cli.client import ApiClient, ApiError

app = typer.Typer(
    name="orchestrator",
    help="Operate the agent-orchestration-engine: run, inspect, and steer executions.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Inspect registered agents.")
app.add_typer(agents_app, name="agents")

console = Console()
# markup=False: every message this prints is raw exception/error text, which
# can itself legitimately contain square brackets (a task description, an
# agent's output) -- treating that as Rich style markup would silently mangle
# or drop it rather than showing the operator what actually went wrong.
error_console = Console(stderr=True, style="bold red", markup=False)

T = TypeVar("T")

#: typer resolves these from $ORCH_API_URL/$ORCH_API_KEY via `envvar=` when the
#: flag is omitted; the literal here is only the final fallback.
ApiUrlOption = Annotated[
    str, typer.Option("--api-url", envvar="ORCH_API_URL", help="Base URL of the running API.")
]
ApiKeyOption = Annotated[
    str | None,
    typer.Option(
        "--api-key", envvar="ORCH_API_KEY", help="X-API-Key header, if the API requires one."
    ),
]


def _run(coro: Coroutine[None, None, T]) -> T:
    """Run one async CLI action, translating API errors into a clean exit.

    A raw traceback is the wrong failure mode for a CLI: an operator wants the
    engine's own error message (already structured by
    :mod:`orchestration.api.errors`), not a stack trace through httpx.
    """
    try:
        return asyncio.run(coro)
    except ApiError as exc:
        error_console.print(str(exc))
        raise typer.Exit(code=1) from exc
    except httpx.ConnectError as exc:
        error_console.print(f"could not reach the API: {exc}")
        raise typer.Exit(code=1) from exc


async def _with_client(
    api_url: str, api_key: str | None, action: Callable[[ApiClient], Awaitable[T]]
) -> T:
    async with ApiClient(api_url, api_key) as client:
        return await action(client)


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="The task description.")],
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
    workflow_id: Annotated[
        str | None, typer.Option(help="Run this registered workflow instead of a dynamic plan.")
    ] = None,
    max_turns: Annotated[
        int | None, typer.Option(help="Cap on supervisor turns for a dynamic execution.")
    ] = None,
    success_criteria: Annotated[
        list[str], typer.Option("--success-criteria", help="Repeatable; what 'done' looks like.")
    ] = [],  # noqa: B006 - typer reads the default directly; not shared/mutated
    wait: Annotated[
        bool, typer.Option(help="Poll until the execution reaches a terminal status.")
    ] = False,
) -> None:
    """Start an execution."""

    async def action(client: ApiClient) -> None:
        created = await client.create_execution(
            task,
            workflow_id=workflow_id,
            max_turns=max_turns,
            success_criteria=tuple(success_criteria),
        )
        console.print(f"[bold]execution_id[/bold]: {escape(str(created['execution_id']))}")
        console.print(f"[bold]status[/bold]: {escape(str(created['status']))}")
        if not wait:
            return
        final = await _poll_until_terminal(client, created["execution_id"])
        _print_execution(final)

    _run(_with_client(api_url, api_key, action))


@app.command()
def status(
    execution_id: str,
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """Show an execution's current state."""

    async def action(client: ApiClient) -> None:
        _print_execution(await client.get_execution(execution_id))

    _run(_with_client(api_url, api_key, action))


@app.command()
def cancel(
    execution_id: str,
    reason: Annotated[str | None, typer.Option(help="Recorded as the cancellation reason.")] = None,
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """Cancel an execution currently in flight."""

    async def action(client: ApiClient) -> None:
        result = await client.cancel_execution(execution_id, reason=reason)
        console.print(result)

    _run(_with_client(api_url, api_key, action))


@app.command()
def resume(
    execution_id: str,
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """Resume an execution stranded by a crashed or restarted process."""

    async def action(client: ApiClient) -> None:
        result = await client.resume_execution(execution_id)
        console.print(result)

    _run(_with_client(api_url, api_key, action))


@app.command()
def approve(
    execution_id: str,
    by: Annotated[str, typer.Option(help="Who is deciding, for the audit trail.")],
    note: Annotated[str | None, typer.Option(help="Optional reviewer note.")] = None,
    approval_id: Annotated[
        str | None, typer.Option(help="Needed only if more than one approval is pending.")
    ] = None,
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """Approve an execution's pending human-approval gate."""

    async def action(client: ApiClient) -> None:
        result = await client.decide_approval(
            execution_id, approve=True, by=by, note=note, approval_id=approval_id
        )
        console.print(result)

    _run(_with_client(api_url, api_key, action))


@app.command()
def reject(
    execution_id: str,
    by: Annotated[str, typer.Option(help="Who is deciding, for the audit trail.")],
    note: Annotated[str | None, typer.Option(help="Optional reviewer note.")] = None,
    approval_id: Annotated[
        str | None, typer.Option(help="Needed only if more than one approval is pending.")
    ] = None,
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """Reject an execution's pending human-approval gate."""

    async def action(client: ApiClient) -> None:
        result = await client.decide_approval(
            execution_id, approve=False, by=by, note=note, approval_id=approval_id
        )
        console.print(result)

    _run(_with_client(api_url, api_key, action))


@agents_app.command("list")
def agents_list(
    api_url: ApiUrlOption = "http://127.0.0.1:8000",
    api_key: ApiKeyOption = None,
) -> None:
    """List every registered agent."""

    async def action(client: ApiClient) -> list[dict[str, object]]:
        return await client.list_agents()

    agents = _run(_with_client(api_url, api_key, action))
    table = Table("id", "name", "kind", "enabled")
    for agent in sorted(agents, key=lambda a: str(a["id"])):
        table.add_row(
            escape(str(agent["id"])),
            escape(str(agent["name"])),
            escape(str(agent["kind"])),
            escape(str(agent["enabled"])),
        )
    console.print(table)


@app.command()
def benchmark(
    category: Annotated[
        list[str], typer.Option("--category", help="Only run this category (repeatable).")
    ] = [],  # noqa: B006 - see the `run` command's success_criteria for why this is safe
    scenario: Annotated[
        list[str], typer.Option("--scenario", help="Only run this scenario id (repeatable).")
    ] = [],  # noqa: B006
    test_db: Annotated[
        bool, typer.Option(help="Use the test database/Redis namespace.")
    ] = False,
    concurrency: Annotated[int, typer.Option(help="Max scenario/arm pairs run at once.")] = 8,
    output: Annotated[Path | None, typer.Option(help="Where to write the JSON report.")] = None,
) -> None:
    """Run the evaluation benchmark directly against the database (no API needed)."""
    from orchestration.cli.benchmark_command import run_benchmark_command

    _run(
        run_benchmark_command(
            categories=category,
            scenario_ids=scenario,
            test_db=test_db,
            concurrency=concurrency,
            output=output,
            console=console,
        )
    )


async def _poll_until_terminal(
    client: ApiClient, execution_id: str, *, interval_seconds: float = 1.0
) -> dict[str, object]:
    terminal = {"succeeded", "failed", "cancelled", "budget_exceeded", "timed_out"}
    while True:
        body = await client.get_execution(execution_id)
        if body["status"] in terminal:
            return body
        console.print(f"  ...status={escape(str(body['status']))}")
        await asyncio.sleep(interval_seconds)


def _print_execution(body: dict[str, object]) -> None:
    console.print(f"[bold]execution_id[/bold]: {escape(str(body.get('execution_id')))}")
    console.print(f"[bold]status[/bold]: {escape(str(body.get('status')))}")
    if body.get("final_output"):
        console.print(f"[bold]final output[/bold]:\n{escape(str(body['final_output']))}")
    if body.get("failure_reason"):
        console.print(f"[bold red]failure reason[/bold red]: {escape(str(body['failure_reason']))}")
    if body.get("pending_approval_id"):
        console.print(
            f"[bold yellow]pending approval[/bold yellow]: "
            f"{escape(str(body['pending_approval_id']))}"
        )


if __name__ == "__main__":
    app()
