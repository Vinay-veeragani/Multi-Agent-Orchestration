# Getting started

## Requirements

- Python 3.11+
- PostgreSQL 16+ with the `vector` extension installed (or installable --
  migrations run `CREATE EXTENSION IF NOT EXISTS vector` for you)
- Redis 7+ (or a Redis-protocol-compatible service)

No LLM API key is required. The engine ships a deterministic mock provider
that drives every test, demo, and benchmark scenario; real provider adapters
(OpenAI, Anthropic, Gemini, Ollama) exist and activate automatically the
moment a credential is configured (see [Configuration](#configuration)).

## Bare-metal setup

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env`:

```bash
ORCH_PG_DSN=postgresql+asyncpg://orchestrator:<password>@127.0.0.1:5432/orchestration
ORCH_REDIS_URL=redis://127.0.0.1:6379/1
```

Create the database and role if they don't exist yet, then apply migrations:

```bash
alembic upgrade head
```

Start the API:

```bash
uvicorn orchestration.api.app:create_app --factory --reload
curl http://127.0.0.1:8000/health
# {"status": "ok", "database": true, "redis": true}
```

## Docker

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
```

Brings up PostgreSQL+pgvector and Redis as their own containers, runs
`alembic upgrade head` once via a `migrate` service, then starts the API.
See [`deployment.md`](deployment.md) for every override variable.

## Verifying the install

```bash
pytest -m unit                 # fast, no network
pytest -m integration          # needs the real PostgreSQL/Redis above
orchestrator agents list
orchestrator run "say hello" --wait
```

## Configuration

Every setting is an `ORCH_`-prefixed environment variable, read by
`orchestration.config.Settings` (pydantic-settings). `.env.example` documents
all of them with defaults; the ones you're most likely to touch first:

| Variable | Purpose |
|---|---|
| `ORCH_PG_DSN` | PostgreSQL connection string (asyncpg) |
| `ORCH_REDIS_URL` | Redis connection string |
| `ORCH_API_KEYS`, `ORCH_API_REQUIRE_AUTH` | `X-API-Key` values the API accepts |
| `ORCH_DEFAULT_PROVIDER` | `mock` until a real key is set |
| `ORCH_OPENAI_API_KEY` / `ORCH_ANTHROPIC_API_KEY` / `ORCH_GEMINI_API_KEY` | Activate a real provider the moment one is set |
| `ORCH_OLLAMA_ENABLED` | Ollama needs no credential, so this explicit flag (not just the base URL) is what opts it in |
| `ORCH_ENABLE_SHELL_TOOL` | Leave `false` -- see `docs/budget-and-policies.md` |
| `ORCH_FILE_SANDBOX_ROOT` | Where file tools are confined to |

Running the evaluation benchmark or the integration test suite against a
*second*, disposable database is standard: `ORCH_PG_TEST_DSN` /
`ORCH_REDIS_TEST_URL` (or `--test-db` on the CLI/benchmark script) point at
that instead, so nothing you run for a quick check ever touches whatever
`ORCH_PG_DSN` points at.

## Next

- [`architecture.md`](architecture.md) for how the pieces fit together.
- [`interfaces.md`](interfaces.md) for the full API and CLI reference.
- [`../examples/`](../examples/) for three narrated, runnable demos.
