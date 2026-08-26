"""SQLAlchemy table definitions.

PostgreSQL-specific by design: ``JSONB`` with GIN indexes, ``vector`` columns for
pgvector, and ``SELECT ... FOR UPDATE SKIP LOCKED`` for node claiming. There is no
SQLite fallback, so nothing here is watered down to the intersection of two
dialects.

Layout decisions worth stating:

**Header rows are separate from state blobs.**
    ``executions`` is queried constantly (list views, status polls) while
    ``execution_states`` holds a large JSONB document. Splitting them means
    answering "is it done yet" does not read a full state document.

**Checkpoints carry a content hash and a unique sequence.**
    ``UNIQUE(execution_id, sequence)`` is what stops two workers both appending a
    "next" checkpoint, and the hash makes a repeated identical write idempotent
    rather than a duplicate row.

**Events are append-only with a unique sequence per execution.**
    Ordering is explicit rather than inferred from timestamps, because concurrent
    parallel branches genuinely share a microsecond.

**Optimistic concurrency on execution state.**
    ``version`` is checked on write. Two processes resuming the same execution
    cannot both advance it; the loser sees a conflict and backs off.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Explicit naming convention so Alembic autogenerate produces stable, readable
#: constraint names instead of database-assigned ones that differ per environment.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB everywhere, with a plain-JSON fallback only so that ``create_all`` on a
#: non-PostgreSQL engine does not explode during a schema-inspection test.
JsonB = JSONB().with_variant(JSON(), "sqlite")

#: Server-side default for optional JSONB columns. ``default=dict`` alone is an
#: ORM-side default, so a raw SQL insert (a migration backfill, an operator fix,
#: a test) would hit a NOT NULL violation. A server default makes the column
#: behave the same however it is written.
EMPTY_JSON = sql_text("'{}'::jsonb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy requires a mutable dict
        dict[str, Any]: JsonB,
    }


def _now() -> Any:
    """Server-side timestamp, so ordering does not depend on client clocks."""
    return func.now()


class AgentRow(Base):
    """A registered agent definition.

    The definition itself is stored as JSONB rather than exploded into columns:
    it is validated by Pydantic on the way in and out, and an agent gains fields
    over time. Indexed columns are only those actually queried.
    """

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    definition: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), onupdate=_now()
    )

    __table_args__ = (Index("ix_agents_definition", "definition", postgresql_using="gin"),)


class ToolRow(Base):
    """A registered tool specification.

    Stored so the API can report what a deployment offers without importing the
    implementations, and so an audit can reconstruct which tool version ran.
    """

    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    spec: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class WorkflowRow(Base):
    """A workflow definition.

    Nodes and edges are also given their own tables. That is deliberate
    duplication: the JSONB document is what executes (and what a checkpoint
    restores), while the relational rows make the graph queryable -- "which
    workflows use this agent" is a question worth being able to ask.
    """

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    dynamic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), onupdate=_now()
    )

    nodes: Mapped[list[WorkflowNodeRow]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )
    edges: Mapped[list[WorkflowEdgeRow]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("name", "version", name="uq_workflows_name_version"),)


class WorkflowNodeRow(Base):
    __tablename__ = "workflow_nodes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)

    workflow: Mapped[WorkflowRow] = relationship(back_populates="nodes")

    __table_args__ = (
        UniqueConstraint("workflow_id", "node_id", name="uq_workflow_nodes_workflow_id"),
    )


class WorkflowEdgeRow(Base):
    __tablename__ = "workflow_edges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edge_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    conditional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)

    workflow: Mapped[WorkflowRow] = relationship(back_populates="edges")

    __table_args__ = (
        UniqueConstraint("workflow_id", "edge_id", name="uq_workflow_edges_workflow_id"),
        Index("ix_workflow_edges_source_target", "workflow_id", "source", "target"),
    )


class ExecutionRow(Base):
    """Header record. Small and heavily queried."""

    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Client-supplied key making execution start idempotent: a retried POST
    #: returns the original execution rather than starting a second one.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), onupdate=_now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_executions_idempotency_key"),
        # Resume scans for stranded executions by status; a partial index keeps
        # that cheap even when the table is mostly finished runs.
        Index(
            "ix_executions_resumable",
            "status",
            "updated_at",
            postgresql_where=None,
        ),
    )


class ExecutionStateRow(Base):
    """The durable state document, one row per execution.

    ``version`` implements optimistic concurrency: a writer supplies the version
    it read, and the update matches on it. Two processes resuming the same
    execution cannot both advance it.
    """

    __tablename__ = "execution_states"

    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    #: The graph in force, which replanning changes.
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), onupdate=_now()
    )

    __table_args__ = (
        CheckConstraint("version >= 0", name="version_non_negative"),
        Index("ix_execution_states_state", "state", postgresql_using="gin"),
    )


class CheckpointRow(Base):
    """An immutable execution snapshot.

    ``UNIQUE(execution_id, sequence)`` is the concurrency guard; ``content_hash``
    makes a repeated identical write detectable so it collapses to one row
    instead of duplicating.
    """

    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_checkpoints_execution_id"),
        Index("ix_checkpoints_execution_sequence", "execution_id", "sequence"),
        Index("ix_checkpoints_hash", "execution_id", "content_hash"),
    )


class ExecutionEventRow(Base):
    """Append-only event log."""

    __tablename__ = "execution_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_execution_events_execution_id"),
        Index("ix_execution_events_execution_sequence", "execution_id", "sequence"),
        Index("ix_execution_events_payload", "payload", postgresql_using="gin"),
    )


class AgentInvocationRow(Base):
    """One agent execution attempt, for audit and per-agent metrics."""

    __tablename__ = "agent_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model_key: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

    __table_args__ = (Index("ix_agent_invocations_exec_node", "execution_id", "node_id"),)


class ToolInvocationRow(Base):
    """One tool invocation attempt.

    ``idempotency_key`` is unique: it is written *before* the call and is what
    stops a resumed execution repeating a side effect that already happened.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    policy_effect: Mapped[str] = mapped_column(String(24), nullable=False, default="allow")
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Redacted before storage; an audit record must not become a secret store.
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JsonB, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
        Index("ix_tool_invocations_exec_tool", "execution_id", "tool"),
    )


class ApprovalRow(Base):
    """A human approval request and its decision."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(256), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    modified_parameters: Mapped[dict[str, Any] | None] = mapped_column(JsonB, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (Index("ix_approvals_pending", "execution_id", "status"),)


class EvidenceChunkRow(Base):
    """A retrievable evidence passage, embedded for pgvector search.

    This is what pgvector is actually for in this system: the critic agent
    checking a claim against retrieved source text, and the research agents
    deduplicating sources they have already seen. The dimension is fixed at
    table-creation time because an HNSW index requires it.
    """

    __tablename__ = "evidence_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("executions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Digest of the content, so the same passage is stored once.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[Any] = mapped_column(Vector(768), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JsonB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

    __table_args__ = (
        UniqueConstraint("content_hash", "source", name="uq_evidence_chunks_content_hash"),
    )


class BenchmarkRunRow(Base):
    """A stored benchmark result, so results are auditable rather than asserted."""

    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    provider_note: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


#: Every table, in dependency order. Used by the test harness to truncate.
ALL_TABLES: tuple[str, ...] = (
    "benchmark_runs",
    "evidence_chunks",
    "approvals",
    "tool_invocations",
    "agent_invocations",
    "execution_events",
    "checkpoints",
    "execution_states",
    "executions",
    "workflow_edges",
    "workflow_nodes",
    "workflows",
    "tools",
    "agents",
)
