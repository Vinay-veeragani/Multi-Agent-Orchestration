# Deployment

## Docker Compose

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
```

Four services:

- **`postgres`** -- `pgvector/pgvector:0.8.6-pg18`, its own named volume.
- **`redis`** -- `redis:7.2-alpine`.
- **`migrate`** -- runs `alembic upgrade head` once and exits; `app` waits
  for it to succeed (`condition: service_completed_successfully`) before
  starting, so there is exactly one place the schema gets created from --
  the same migrations the bare-metal setup uses.
- **`app`** -- the API (`uvicorn orchestration.api.app:create_app
  --factory`), waits on `migrate` and on Redis being healthy.

### Overriding defaults without touching the bare-metal `.env`

Compose CLI auto-loads a top-level `.env` for `${VAR}` substitution -- and
this repo already has one, for the bare-metal setup, full of `ORCH_*`
variables. Every value in `docker-compose.yml` a user might reasonably want
to override therefore uses a `COMPOSE_*`-prefixed name instead of the
`ORCH_*` name the container actually receives, so the compose defaults are
never silently shadowed by settings meant for a host-installed
PostgreSQL/Redis:

```bash
cp docker/.env.docker.example docker/.env.docker
# edit COMPOSE_POSTGRES_PASSWORD, COMPOSE_API_KEYS, etc.
docker compose --env-file docker/.env.docker up --build
```

| Variable | Controls |
|---|---|
| `COMPOSE_POSTGRES_USER` / `_PASSWORD` / `_DB` | The `postgres` container's credentials |
| `COMPOSE_POSTGRES_PORT` / `_REDIS_PORT` / `_API_PORT` | Host port mappings |
| `COMPOSE_API_KEYS` | `X-API-Key` values the deployed API accepts |
| `COMPOSE_OPENAI_API_KEY` / `_ANTHROPIC_API_KEY` / `_GEMINI_API_KEY` | Activate a real provider inside the container |

`ORCH_API_REQUIRE_AUTH` is fixed to `true` in the compose file itself (not
parameterised) -- a deliberate secure default; edit the compose file directly
if you need it off for a throwaway local trial.

### The image

Two-stage build (`Dockerfile`): a `builder` stage compiles the package and
its dependencies into a throwaway virtualenv; the `runtime` stage copies
only that venv plus what the running app actually needs beyond the
importable package -- `alembic.ini`, `migrations/`, `benchmarks/` -- into a
slim final image, running as a non-root user. No compiler, no dev tooling
(ruff/mypy/pytest), ships in the image that actually runs.

`migrate` and `app` are the *same* image with different `command:`s --
there's no image-selection logic baked in; compose decides which role each
container plays.

## Production configuration notes

- **Set real `ORCH_API_KEYS`** before exposing the API beyond localhost --
  the shipped default is a placeholder, not a secret.
- **`ORCH_TRACING_ENABLED=true` + `ORCH_TRACE_EXPORTER=otlp`** once a
  collector is reachable; tracing degrades to a safe no-op otherwise (see
  [`observability.md`](observability.md)), so turning it on is never
  destructive to try.
- **`ORCH_LOG_FORMAT=json`** in any environment with a log aggregator;
  `console` is for a human watching a terminal.
- **Concurrency limits** (`ORCH_MAX_CONCURRENT_EXECUTIONS`,
  `_MAX_CONCURRENT_AGENTS`, `_MAX_CONCURRENT_TOOLS`) are enforced via Redis
  semaphores -- deployment-wide, not per-process -- size them to what the
  configured database connection pool (`ORCH_PG_POOL_SIZE`) can actually
  sustain.
- **Single-process execution ownership.** `ExecutionRunner` drives
  executions as background tasks within one API process; cross-process
  cancellation propagation (a different worker cancelling an execution
  running elsewhere) is a documented known limitation, not a silent gap --
  see the root README's "What this project is NOT".
- **Never set `ORCH_ENABLE_SHELL_TOOL=true`** in a shared or
  internet-reachable deployment; see
  [`budget-and-policies.md`](budget-and-policies.md).

## See also

- [`getting-started.md`](getting-started.md) -- bare-metal setup, for
  comparison.
- [`interfaces.md`](interfaces.md) -- what the deployed API and CLI can do.
