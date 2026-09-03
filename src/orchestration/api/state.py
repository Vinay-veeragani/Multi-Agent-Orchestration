"""Process-wide dependencies the API hands to every route.

Built once at startup and torn down at shutdown (see :mod:`orchestration.api.
app`'s lifespan). Everything here is either read-mostly (registries, the policy
engine) or manages its own concurrency internally (the database engine, the
Redis client) -- nothing requires a lock to be shared safely across requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from orchestration.agents.definitions import REFERENCE_AGENTS, build_default_agent_registry
from orchestration.agents.registry import AgentRegistry
from orchestration.api.runner import ExecutionRunner
from orchestration.checkpoint.manager import CheckpointManager
from orchestration.config import Settings, get_settings
from orchestration.coordination.redis import ConcurrencyLimiter, RedisCoordinator
from orchestration.llm.factory import (
    LLMClient,
    build_provider_builders,
    configured_providers,
    resolve_pinned_model_key,
)
from orchestration.persistence.database import Database
from orchestration.persistence.repositories import (
    AgentRepository,
    ProviderCredentialRepository,
    RoutingSettingsRepository,
    ToolRepository,
)
from orchestration.policies.engine import PolicyEngine, build_default_policy_engine
from orchestration.routing.model_router import ModelRouter, build_default_router
from orchestration.tools.mcp import MCPClient, discover_mcp_tools
from orchestration.tools.registry import ToolRegistry, build_default_registry


@dataclass(slots=True)
class AppState:
    """Every shared collaborator a route or the execution runner needs."""

    settings: Settings
    database: Database
    redis: RedisCoordinator
    limiter: ConcurrencyLimiter
    agents: AgentRegistry
    tools: ToolRegistry
    policy: PolicyEngine
    llm: LLMClient
    router: ModelRouter
    checkpoint_manager: CheckpointManager
    sandbox_root: Path
    #: Open MCP connections, closed alongside everything else in
    #: close_app_state. Empty unless settings.mcp_enabled.
    mcp_clients: list[MCPClient] = field(default_factory=list)
    runner: ExecutionRunner = field(init=False)

    def __post_init__(self) -> None:
        self.runner = ExecutionRunner(self)

    async def reload_providers(self) -> None:
        """Rebuild the LLM client and router from settings plus stored credentials.

        Called once during startup and again every time `PUT`/`DELETE
        /providers/{name}` changes a row -- a key entered through the
        Providers page must take effect on the next call this process makes,
        not after a restart, which is the whole point of storing it in
        `provider_credentials` instead of only ever reading `.env`.

        Executions already in flight keep whichever `LLMClient`/`ModelRouter`
        instance they were constructed with (see `ExecutionRunner._run_engine`,
        which reads `self._app.llm`/`.router` fresh per execution) -- only a
        *new* execution picks up a just-changed credential.

        When an "active provider" is set (see `RoutingSettingsRepository`),
        every other connected provider is excluded from routing entirely --
        exclusivity is the point of choosing one, not just a tiebreaker among
        several. With none set, every connected provider stays eligible and
        the router's usual cost-aware selection decides per call, same as
        before this setting existed.
        """
        async with self.database.session() as session:
            rows = await ProviderCredentialRepository(session).list_all()
            active_provider = await RoutingSettingsRepository(session).get_active_provider()
        overrides = {row["provider"]: row for row in rows}

        available = configured_providers(self.settings, overrides=overrides)
        if active_provider and active_provider in available:
            available = tuple(p for p in available if p in ("mock", active_provider))
        mock_only = tuple(available) == ("mock",)
        builders = build_provider_builders(self.settings, overrides=overrides)
        active_row = overrides.get(active_provider) if active_provider else None

        old_llm = self.llm
        self.llm = LLMClient(builders=builders)
        self.router = build_default_router(
            mock_only=mock_only,
            configured_providers=available,
            force_model_key=resolve_pinned_model_key(self.settings, active_row),
        )
        await old_llm.aclose()


async def build_app_state(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    database: Database | None = None,
    redis: RedisCoordinator | None = None,
) -> AppState:
    """Assemble and warm up every shared collaborator.

    Seeds the database with the built-in reference agents so ``GET /agents``
    is useful on a freshly migrated database without a separate seed script --
    the same agents the engine ships with are the ones the API reports.

    ``llm``/``database``/``redis`` are overridable so tests can inject a
    scripted :class:`~orchestration.llm.mock.MockProvider`-backed client and a
    test database/Redis namespace, the same way every other test harness in
    this project does, rather than the production app always reaching for
    real credentials and the default connection settings.
    """
    resolved = settings or get_settings()
    database = database or Database(resolved.pg_dsn, settings=resolved)
    redis = redis or RedisCoordinator(resolved.redis_url, settings=resolved)
    limiter = ConcurrencyLimiter(
        redis,
        max_executions=resolved.max_concurrent_executions,
        max_agents=resolved.max_concurrent_agents,
        max_tools=resolved.max_concurrent_tools,
    )

    agents = build_default_agent_registry()
    tools = build_default_registry()

    mcp_clients: list[MCPClient] = []
    if resolved.mcp_enabled and resolved.mcp_server_command:
        # Deliberately not swallowed: an operator who turned this on should
        # learn immediately that the configured server is unreachable, not
        # silently run with fewer tools than they think they have.
        client = MCPClient(
            resolved.mcp_server_command, timeout_seconds=resolved.mcp_timeout_seconds
        )
        await client.start()
        mcp_clients.append(client)
        for tool in await discover_mcp_tools(client, server_name=resolved.mcp_server_name):
            tools.register(tool)

    policy = build_default_policy_engine(agents=agents, tools=tools)

    llm_was_injected = llm is not None
    available = configured_providers(resolved)
    mock_only = tuple(available) == ("mock",)
    llm = llm or LLMClient()
    router = build_default_router(
        mock_only=mock_only,
        configured_providers=available,
        force_model_key=resolved.pinned_model_key,
    )

    async with database.session() as session:
        agent_repo = AgentRepository(session)
        tool_repo = ToolRepository(session)
        for definition in REFERENCE_AGENTS:
            await agent_repo.upsert(definition)
        for spec in tools.list_specs(include_disabled=True):
            await tool_repo.upsert(spec, enabled=tools.is_enabled(spec.name))

    app_state = AppState(
        settings=resolved,
        database=database,
        redis=redis,
        limiter=limiter,
        agents=agents,
        tools=tools,
        policy=policy,
        llm=llm,
        router=router,
        checkpoint_manager=CheckpointManager(database),
        sandbox_root=resolved.file_sandbox_root,
        mcp_clients=mcp_clients,
    )
    if llm_was_injected:
        # A test (or another caller) supplied its own LLMClient -- typically a
        # scripted MockProvider -- specifically to keep real credentials and
        # DB-stored overrides out of it. Folding provider_credentials in here
        # would silently replace that intentional substitution.
        return app_state
    await app_state.reload_providers()
    return app_state


async def close_app_state(state: AppState) -> None:
    await state.runner.shutdown()
    for client in state.mcp_clients:
        await client.aclose()
    await state.database.aclose()
    await state.redis.aclose()
