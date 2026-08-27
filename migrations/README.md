# Database migrations

Alembic migrations for the orchestration engine's PostgreSQL schema, targeting
`orchestration.persistence.tables.Base.metadata` -- the same declarative base
the application reads and writes through at runtime. Autogenerate diffs
against what the ORM actually declares, so there is one schema description,
not two that can drift apart.

## Connection

Neither `alembic.ini` nor this directory contains a connection string.
`env.py` resolves the DSN at runtime from `ORCH_PG_MIGRATIONS_DSN` if set,
otherwise from `ORCH_PG_DSN` (the same setting the application uses) via
`orchestration.config.get_settings()`. Point migrations at a different
database than the running application by setting `ORCH_PG_MIGRATIONS_DSN`.

## Common commands

Run from the repository root, with the project's virtual environment active:

```bash
# Apply every migration up to the latest.
alembic upgrade head

# Roll back everything (drops all application tables; never drops the
# `vector` extension itself -- see the baseline migration's downgrade()).
alembic downgrade base

# Show the currently applied revision.
alembic current

# Verify the live schema has zero drift from the ORM (exits non-zero on
# any difference -- useful as a CI gate after changing a table).
alembic check

# After changing a table in orchestration/persistence/tables.py, generate
# a new migration from the diff. Always read the generated file before
# committing it -- autogenerate is a starting point, not a guarantee.
alembic revision --autogenerate -m "describe the change"
```

## Baseline migration

`versions/..._baseline_schema.py` creates every table in one revision,
including `CREATE EXTENSION IF NOT EXISTS vector` before the
`evidence_chunks.embedding` column (a `vector(768)`) is created. The
extension step is idempotent, so upgrading against a database that already
has pgvector installed manually does not fail. Its `downgrade()` deliberately
does **not** drop the extension: another schema, or a manual install, may
depend on it, and removing an extension is a heavier and more surprising
operation than a reference implementation should perform as a side effect of
rolling back its own tables.

## Testing

`tests/integration/test_migrations.py` runs `upgrade head` and
`downgrade base` against the real test database (not `create_all()`) and
asserts, via `alembic.autogenerate.compare_metadata`, that the migrated
schema has zero drift from `Base.metadata` -- the same check `alembic check`
performs, run in-process so it participates in the normal test suite.
