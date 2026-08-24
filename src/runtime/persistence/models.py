"""SQLAlchemy models.

These mirror the migration DDL exactly — the migrations are the authority, these
are the typed view of them. `tests/test_migrations.py::test_models_match_schema`
compares the two so they cannot drift.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        uuid.UUID: PGUUID(as_uuid=True),
        dt.datetime: TZDateTime,
    }


# --- 001 foundations ---------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # --- 016 authority ---
    # An actor's place in the hierarchy. Nullable because an actor with no role
    # resolves to default-deny for every gated action, which is the correct answer
    # for one nobody has placed yet.
    role_name: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    active_version_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_actor_name"),)


class ActorVersion(Base):
    """An immutable authored spec. Runs reference a version, never an actor.

    `spec_hash` is stored so that "did these two runs use the same configuration"
    is an index lookup rather than a JSON diff.
    """

    __tablename__ = "actor_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("actor_id", "version", name="uq_actor_version"),)


# --- 002 runs ----------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    root_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id"), nullable=False)
    actor_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("actor_versions.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="50")
    worker_id: Mapped[uuid.UUID | None] = mapped_column()
    lease_until: Mapped[dt.datetime | None] = mapped_column()
    task_id: Mapped[uuid.UUID | None] = mapped_column()
    """M1, migration 009. Nullable, and the nullability is load-bearing: spend with
    no task attached is exactly the coordination overhead the gate measures."""
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lease_expiries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deadline: Mapped[dt.datetime | None] = mapped_column()
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column()
    ended_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        # Load-bearing for cron dedupe, stream redelivery and API retries at the
        # same time. Never make this partial or nullable.
        UniqueConstraint("organization_id", "idempotency_key", name="uq_run_idem"),
        Index(
            "ix_runs_claimable",
            "status",
            "lease_until",
            postgresql_where="status IN ('QUEUED','RUNNING')",
        ),
        Index("ix_runs_root", "root_run_id"),
    )


class RunSpecRow(Base):
    """The frozen compiled spec for one run. Written in the same transaction as the
    run row; read by the worker instead of live config."""

    __tablename__ = "run_specs"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)


# --- 003 outbox / events -----------------------------------------------------------


class Outbox(Base):
    """Transactional outbox.

    A row is written in the same transaction as the state change it announces, so
    "the run exists" and "the run was announced" cannot disagree. The relay is then
    free to be at-least-once, because `uq_outbox_dedupe` makes the write idempotent
    and `published_at` makes the publish idempotent.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        UniqueConstraint("topic", "dedupe_key", name="uq_outbox_dedupe"),
        Index("ix_outbox_unpublished", "id", postgresql_where="published_at IS NULL"),
    )


class Event(Base):
    """Durable event log. The SSE tail reads from here, not from Redis, so a
    reconnecting client can replay from any point and a Redis wipe (R1) costs
    nothing."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    root_run_id: Mapped[uuid.UUID | None] = mapped_column()
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("topic", "dedupe_key", name="uq_event_dedupe"),
        Index("ix_events_run", "run_id", "id"),
    )


# --- 004 effects -------------------------------------------------------------------


class EffectIntent(Base):
    """One row per logical side effect.

    `logical_call_id` is unique. That uniqueness, plus the fact that the INTENT row
    commits in its own transaction *before* the effect fires, is the whole
    exactly-once mechanism. Everything else is policy about what to do when a
    replay finds one of these already here.
    """

    __tablename__ = "effect_intents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    logical_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    root_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    args_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    recovery_policy: Mapped[str] = mapped_column(Text, nullable=False)
    blast_radius: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    marker: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[uuid.UUID | None] = mapped_column()
    result_inline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("logical_call_id", name="uq_effect_logical_call"),
        Index("ix_effects_run", "run_id"),
        Index("ix_effects_open", "status", postgresql_where="status = 'INTENT'"),
    )


class SideEffectFixture(Base):
    """Test-only observation surface.

    `fixture.sideeffect@1` writes here. It exists purely so the exactly-once test
    has something outside the journal to count. `marker` is unique so a genuine
    double-execution shows up as an integrity error rather than a second row that
    a buggy assertion might miss.
    """

    __tablename__ = "sideeffect_fixture"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    marker: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    logical_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("marker", name="uq_sideeffect_marker"),)


class AuditLog(Base):
    """Append-only record of every gateway decision that mattered."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    root_run_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    fence: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="normal")
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_audit_run", "run_id", "id"),)


# --- 005 artifacts -----------------------------------------------------------------


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_artifacts_run", "run_id"),)


class ArtifactVersion(Base):
    """A version row exists only after the bytes are durably in the object store.

    Order matters: bytes first, row second. The reverse would let a reader follow a
    reference to nothing. A row with no bytes is a bug; bytes with no row are
    garbage the sweeper collects.
    """

    __tablename__ = "artifact_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("artifacts.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),)


class ArtifactLink(Base):
    __tablename__ = "artifact_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("artifacts.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "source_type", "source_id", "relation", name="uq_artifact_link"
        ),
    )


# --- 006 budget --------------------------------------------------------------------


class BudgetPool(Base):
    """I8 enforced by the database.

    `ck_budget_invariant` turns a bug in reservation logic into a failed
    transaction instead of a silent overspend. Worth the constraint-violation noise
    it occasionally produces in tests.
    """

    __tablename__ = "budget_pools"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    period: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    limit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reserved_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    allocated_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    """Sum of LIVE allocation ceilings across this pool's whole subtree.

    The one denormalised number in the budget tables, created by 006 and unused until
    M2. Maintained only by `open_allocation`/`close_allocation`, both of which take the
    chain lock first, and repairable with `reconcile_allocated_cents`. It exists
    because the honest query — a recursive descent per level, on every admission —
    would be on the hot path."""
    # --- 017 budget tree ---
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("budget_pools.id"))
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    """Denormalised because it is the primary key of the lock order
    (`ORDER BY depth ASC, id ASC`), and an order derived inside the locking statement
    would depend on a subquery the planner may reorder."""

    __table_args__ = (
        CheckConstraint(
            "committed_cents + reserved_cents <= limit_cents", name="ck_budget_invariant"
        ),
        CheckConstraint("committed_cents >= 0 AND reserved_cents >= 0", name="ck_budget_signs"),
        CheckConstraint("depth >= 0 AND depth < 16", name="ck_budget_depth"),
        CheckConstraint(
            "(depth = 0 AND parent_id IS NULL) OR (depth > 0 AND parent_id IS NOT NULL)",
            name="ck_budget_root",
        ),
        UniqueConstraint(
            "scope_type", "scope_id", "period", "period_start", name="uq_budget_pool_scope"
        ),
        Index("ix_budget_pool_parent", "parent_id"),
    )


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    pool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("budget_pools.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_res_sweep", "expires_at", postgresql_where="status = 'HELD'"),
        Index("ix_res_run", "run_id"),
    )


class UsageLedger(Base):
    """What was actually spent, by work class. Append-only."""

    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    root_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    pool_id: Mapped[uuid.UUID | None] = mapped_column()
    reservation_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    work_class: Mapped[str | None] = mapped_column(Text)
    call_site: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_usage_run", "run_id"),)


# --- 008 goals ---------------------------------------------------------------------


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    horizon: Mapped[str] = mapped_column(Text, nullable=False, server_default="quarter")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_goal_name"),)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    owner_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_project_name"),
        Index("ix_projects_goal", "goal_id"),
    )


# --- 009 tasks ---------------------------------------------------------------------


class Task(Base):
    """One unit of assignable work with a pinned output contract.

    The two counters are capped by CHECK constraints rather than by application
    logic, which means a bug that tries to open a fourth rework cycle fails its
    transaction instead of quietly running one. T15 and T16 are about those caps.

    `version` plus `lease_worker`/`lease_until` are the task's own optimistic-
    concurrency triple — deliberately not the run's fence. A run is executed by one
    worker for minutes; a task is owned by one assignee across an entire submit /
    evaluate / rework cycle, and T21 is a race between two workers claiming the
    same *task*.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    goal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("goals.id"))
    correlation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    assignee_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    assignee_name: Mapped[str | None] = mapped_column(Text)
    input_schema_ref: Mapped[str | None] = mapped_column(Text)
    output_schema_ref: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    rework_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    schema_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_schema_errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    human_touched: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    lease_worker: Mapped[uuid.UUID | None] = mapped_column()
    lease_until: Mapped[dt.datetime | None] = mapped_column()
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    due_at: Mapped[dt.datetime | None] = mapped_column()
    submitted_at: Mapped[dt.datetime | None] = mapped_column()
    evaluated_at: Mapped[dt.datetime | None] = mapped_column()
    eval_deadline: Mapped[dt.datetime | None] = mapped_column()
    closed_at: Mapped[dt.datetime | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','ASSIGNED','IN_PROGRESS','SUBMITTED','CLOSED','CANCELLED')",
            name="ck_task_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS',"
            "'REWORK_REQUIRED','REJECTED','AUTO_ACCEPTED')",
            name="ck_task_outcome",
        ),
        CheckConstraint("rework_count >= 0 AND rework_count <= 2", name="ck_task_rework_cap"),
        CheckConstraint("schema_failures >= 0 AND schema_failures <= 3", name="ck_task_schema_cap"),
        Index("ix_tasks_status", "status", "assignee_name"),
        Index("ix_tasks_correlation", "correlation_id"),
        Index(
            "ix_tasks_eval_due",
            "eval_deadline",
            postgresql_where="status = 'SUBMITTED' AND outcome IS NULL",
        ),
        Index("ix_tasks_created", "organization_id", "created_at"),
    )


# --- 010 evaluations ---------------------------------------------------------------


class TaskEvaluation(Base):
    """The manager's verdict and the human's sample, in one table.

    `evaluator_kind` is the only thing distinguishing them, which is what makes the
    §8.2 confusion matrix a self-join rather than a reconciliation between two
    stores that will eventually disagree.
    """

    __tablename__ = "task_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    evaluator_kind: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    evaluator_ref: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    rubric: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    rework_instructions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    edit_distance: Mapped[int | None] = mapped_column(Integer)
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("evaluator_kind IN ('manager','human','system')", name="ck_evaluator_kind"),
        CheckConstraint(
            "outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS','REWORK_REQUIRED',"
            "'REJECTED','AUTO_ACCEPTED')",
            name="ck_evaluation_outcome",
        ),
        UniqueConstraint("task_id", "evaluator_kind", "attempt", name="uq_evaluation_once"),
        Index("ix_evaluations_task", "task_id"),
        Index("ix_evaluations_kind", "evaluator_kind", "created_at"),
    )


# --- 011 inbox ---------------------------------------------------------------------


class InboxMessage(Base):
    """The durable queue between actors.

    Because it is in Postgres rather than only in Redis, actor-to-actor delivery
    survives R1 for free: a `FLUSHALL` loses the notification, and the dispatcher's
    next poll finds the message still PENDING.
    """

    __tablename__ = "inbox_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    causation_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sender_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    sender_name: Mapped[str | None] = mapped_column(Text)
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column()
    artifact_id: Mapped[uuid.UUID | None] = mapped_column()
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    hop_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    drop_reason: Mapped[str | None] = mapped_column(Text)
    delivered_run_id: Mapped[uuid.UUID | None] = mapped_column()
    delivered_at: Mapped[dt.datetime | None] = mapped_column()
    read_at: Mapped[dt.datetime | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_inbox_dedupe"),
        CheckConstraint("status IN ('PENDING','DELIVERED','DROPPED')", name="ck_inbox_status"),
        CheckConstraint("hop_count >= 0 AND hop_count <= 8", name="ck_inbox_hop_cap"),
        Index("ix_inbox_undelivered", "created_at", postgresql_where="status = 'PENDING'"),
        Index("ix_inbox_recipient", "recipient_name", "created_at"),
        Index("ix_inbox_correlation", "correlation_id"),
        Index(
            "ix_inbox_session",
            "session_id",
            "created_at",
            postgresql_where="session_id IS NOT NULL",
        ),
    )


# --- 012 sessions ------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    session_key: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="OPEN")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summarized_upto: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("organization_id", "session_key", name="uq_session_key"),
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_session_status"),
        Index("ix_sessions_actor", "actor_name", "created_at"),
    )


class SessionSummary(Base):
    """Append-only. `upto_message_count` is what makes "what did the actor know at
    the time" answerable after the fact — a summary that overwrote a column would
    not."""

    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    upto_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "upto_message_count", name="uq_session_summary_upto"),
        Index("ix_session_summaries", "session_id", "id"),
    )


# --- 013 triggers ------------------------------------------------------------------


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    catchup_policy: Mapped[str] = mapped_column(Text, nullable=False, server_default="skip")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_evaluated_at: Mapped[dt.datetime | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_trigger_key"),
        CheckConstraint("catchup_policy IN ('skip','all')", name="ck_trigger_catchup"),
        Index("ix_triggers_active", "active", postgresql_where="active"),
    )


class TriggerFire(Base):
    """Dedupe table and catch-up ledger at once.

    The primary key is `H(trigger_id, scheduled_for_utc)` — a natural key, so there
    is no surrogate id. Racing schedulers, redelivery and merged wakes all collapse
    into one conflict on it.
    """

    __tablename__ = "trigger_fires"

    trigger_key: Mapped[str] = mapped_column(Text, primary_key=True)
    trigger_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("triggers.id"), nullable=False)
    scheduled_for: Mapped[dt.datetime] = mapped_column(nullable=False)
    fired_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    correlation_id: Mapped[uuid.UUID | None] = mapped_column()

    __table_args__ = (Index("ix_trigger_fires_trigger", "trigger_id", "scheduled_for"),)


# --- 014 approvals -----------------------------------------------------------------


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_by_run_id: Mapped[uuid.UUID | None] = mapped_column()
    requested_by_actor: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column()
    approver: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    on_expiry: Mapped[str] = mapped_column(Text, nullable=False, server_default="deny")
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decision_note: Mapped[str | None] = mapped_column(Text)
    escalation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Incremented as of M2 — the column 014 created so escalation would be logic
    rather than an ALTER TABLE on a hot table at the worst moment."""
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    # --- 018 approvals v2 ---
    escalation_chain: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    """A JSON *array* of approver names. Typed as JSONB either way; the annotation
    map has no separate entry for a list, and inventing one would be a type that
    exists to satisfy a mapper rather than to describe the column."""
    escalation_index: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_escalations: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="86400")
    """Frozen from the policy, not re-read at escalation time — and a column rather
    than `expires_at - created_at`, which escalation rewrites."""
    deferred_until: Mapped[dt.datetime | None] = mapped_column()
    resume_token: Mapped[str | None] = mapped_column(Text)
    interrupt_id: Mapped[str | None] = mapped_column(Text)
    resume_token_used_at: Mapped[dt.datetime | None] = mapped_column()
    resume_run_id: Mapped[uuid.UUID | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "action", name="uq_approval_subject"),
        UniqueConstraint("resume_token", name="uq_approval_resume_token"),
        CheckConstraint(
            "status IN ('PENDING','GRANTED','DENIED','EXPIRED')", name="ck_approval_status"
        ),
        CheckConstraint(
            "on_expiry IN ('deny','grant','escalate','fail_run')", name="ck_approval_on_expiry"
        ),
        CheckConstraint(
            "escalation_index >= 0 AND escalation_index <= max_escalations",
            name="ck_approval_escalation",
        ),
        Index("ix_approvals_pending", "expires_at", postgresql_where="status = 'PENDING'"),
        Index("ix_approvals_subject", "subject_type", "subject_id"),
        Index(
            "ix_approvals_queue",
            "organization_id",
            "approver",
            "expires_at",
            postgresql_where="status = 'PENDING'",
        ),
    )


# --- 016 authority -----------------------------------------------------------------


class Role(Base):
    """A position in the hierarchy. The parent pointer is the escalation order.

    `rank` is advisory and `parent_role_id` is authoritative; they are checked against
    each other by `check_escalation_acyclicity` rather than trusted separately, because
    two columns that can disagree eventually will.
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text)
    parent_role_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("roles.id", name="fk_role_parent", ondelete="SET NULL")
    )
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="100")
    approver: Mapped[str | None] = mapped_column(Text)
    approver_daily_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    base_authority: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_role_name"),
        CheckConstraint("approver_daily_budget >= 0", name="ck_role_budget_sign"),
        Index("ix_roles_org", "organization_id"),
    )


class AuthorityPolicy(Base):
    __tablename__ = "authority_policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    approver_role: Mapped[str | None] = mapped_column(Text)
    max_escalations: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    on_expiry: Mapped[str] = mapped_column(Text, nullable=False, server_default="deny")
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="86400")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "scope_type", "scope_id", "action", name="uq_authority_scope"
        ),
        CheckConstraint(
            "scope_type IN ('role','department','actor')", name="ck_authority_scope_type"
        ),
        CheckConstraint("level IN ('auto','human','denied')", name="ck_authority_level"),
        CheckConstraint(
            "on_expiry IN ('deny','grant','escalate','fail_run')", name="ck_authority_on_expiry"
        ),
        CheckConstraint("ttl_seconds > 0", name="ck_authority_ttl"),
        CheckConstraint("max_escalations >= 0", name="ck_authority_escalations"),
        Index("ix_authority_org", "organization_id", "action"),
    )


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")
    credential_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_connection_name"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_connection_status"),
    )


class ToolGrant(Base):
    """Revoked, never deleted — "who could do what last Tuesday" is a question the
    §9 denial review actually asks."""

    __tablename__ = "tool_grants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id"))
    granted_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="system")
    granted_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column()
    revoked_at: Mapped[dt.datetime | None] = mapped_column()
    revoked_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "subject_type", "subject_id", "tool", name="uq_tool_grant"
        ),
        CheckConstraint("subject_type IN ('role','actor')", name="ck_grant_subject_type"),
        Index(
            "ix_tool_grants_live",
            "organization_id",
            "subject_type",
            "subject_id",
            postgresql_where="revoked_at IS NULL",
        ),
    )


# --- 017 budget tree ---------------------------------------------------------------


class BudgetAllocation(Base):
    """Advisory (v3 §8): declares intent, holds nothing.

    That is what makes soft oversubscription to `limit x (1 + K)` safe — the hard
    reservation path is still bounded by `limit` at every level, so 1.3x of intent
    cannot become 1.3x of money. T31.
    """

    __tablename__ = "budget_allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    pool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("budget_pools.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ceiling_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default="NORMAL")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="LIVE")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_alloc_run"),
        CheckConstraint("status IN ('LIVE','CLOSED')", name="ck_alloc_status"),
        CheckConstraint("priority IN ('LOW','NORMAL','CRITICAL')", name="ck_alloc_priority"),
        CheckConstraint("ceiling_cents >= 0", name="ck_alloc_ceiling_sign"),
        Index("ix_alloc_live", "pool_id", postgresql_where="status = 'LIVE'"),
    )


# --- 018 approvals v2 --------------------------------------------------------------


class ApproverBudget(Base):
    """Assignments per approver per day. Counted to *defer*, never to deny.

    The number is assignments rather than decisions on purpose: what produces
    rubber-stamping is the length of the queue somebody is handed, not how many of it
    they got through (edge case 31).
    """

    __tablename__ = "approver_budgets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    approver: Mapped[str] = mapped_column(Text, nullable=False)
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    alerted_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("organization_id", "approver", "day", name="uq_approver_day"),
        CheckConstraint("assigned >= 0 AND daily_limit >= 0", name="ck_approver_budget_signs"),
    )


# --- 019 kill switch ---------------------------------------------------------------


class KillSwitchRecord(Base):
    __tablename__ = "kill_switches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    engaged_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="operator")
    engaged_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    disengaged_at: Mapped[dt.datetime | None] = mapped_column()
    disengaged_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Mirrors 036. `target_metadata = Base.metadata` (migrations/env.py), so a
        # model left at 019's four values would make the next `--autogenerate`
        # propose reverting the constraint.
        CheckConstraint(
            "scope_type IN ('org','tool','actor','connection','department')",
            name="ck_kill_scope_type",
        ),
        CheckConstraint("mode IN ('halt','drain')", name="ck_kill_mode"),
        CheckConstraint(
            "(scope_type = 'org' AND scope_id IS NULL) OR "
            "(scope_type <> 'org' AND scope_id IS NOT NULL)",
            name="ck_kill_scope_id",
        ),
        Index("ix_kill_active", "organization_id", postgresql_where="disengaged_at IS NULL"),
        # One live switch per scope. Two, in different modes, is an ambiguity nobody
        # resolves correctly under incident pressure — so it is unrepresentable.
        Index(
            "ux_kill_live",
            "organization_id",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_where="disengaged_at IS NULL AND scope_id IS NOT NULL",
        ),
        Index(
            "ux_kill_live_org",
            "organization_id",
            "scope_type",
            unique=True,
            postgresql_where="disengaged_at IS NULL AND scope_id IS NULL",
        ),
    )


# --- 021 rate limits ---------------------------------------------------------------


class RateLimitPolicyRow(Base):
    __tablename__ = "rate_limit_policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    limit_per_window: Mapped[int] = mapped_column(Integer, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    burst: Mapped[int] = mapped_column(Integer, nullable=False)
    fail_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "scope_type", "scope_id", name="uq_rate_limit_scope"),
        CheckConstraint(
            "scope_type IN ('connection','provider','actor')", name="ck_rate_scope_type"
        ),
        CheckConstraint("limit_per_window > 0 AND window_seconds > 0", name="ck_rate_positive"),
        CheckConstraint("burst >= limit_per_window", name="ck_rate_burst"),
    )


# --- 022 credentials ---------------------------------------------------------------


class CredentialRecord(Base):
    """Ciphertext only. The key lives in the process environment, not in this table —
    see `runtime.gateway.credentials` for what that boundary does and does not buy."""

    __tablename__ = "credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    rotated_at: Mapped[dt.datetime | None] = mapped_column()
    rotated_by: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column()
    last_fetched_at: Mapped[dt.datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", name="uq_credential_version"),
        CheckConstraint("status IN ('ACTIVE','RETIRED')", name="ck_credential_status"),
        CheckConstraint("version > 0", name="ck_credential_version"),
        Index(
            "uq_credential_active",
            "organization_id",
            "name",
            unique=True,
            postgresql_where="status = 'ACTIVE'",
        ),
    )


# --- 023-029 memory ------------------------------------------------------------------
#
# The vector data itself has no model here, and that is deliberate. It lives inside the
# `mem` schema in tables the store adapter creates at runtime, named after an
# `embedding_version` that no migration can know in advance (§3.3). What is modelled is
# the half we own: the sidecar, the audit, and everything the policy reads.


class MemoryCapabilities(Base):
    """One row, written by migration 023: can this database do ANN at all?

    Read at store construction so that "we are running the native exact-cosine store
    because pgvector is absent" is a startup log line rather than something inferred
    from a latency graph six weeks later.
    """

    __tablename__ = "capabilities"
    __table_args__ = (
        CheckConstraint("id = true", name="ck_capabilities_singleton"),
        {"schema": "mem"},
    )

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    pgvector: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pgvector_version: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)


class MemoryCollection(Base):
    """The collection registry. §3.3's "name the collection with the version".

    `dim` is here rather than only in a pgvector column type because the native
    adapter's `real[]` column would otherwise accept any width, and T45 has to test a
    constraint that exists.
    """

    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint("dim > 0", name="ck_collection_dim"),
        CheckConstraint("backend IN ('native','mem0')", name="ck_collection_backend"),
        Index("ix_mem_collection_version", "embedding_version"),
        {"schema": "mem"},
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_version: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class MemoryMetadata(Base):
    """The sidecar. Scope, trust, provenance and promotion state — our policy, not the
    library's (§4), and in `public` so that re-seeding the vector store cannot take the
    provenance with it."""

    __tablename__ = "memory_metadata"

    memory_id: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    trust: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column()
    source_actor_id: Mapped[uuid.UUID | None] = mapped_column()
    source_actor_version: Mapped[int | None] = mapped_column(BigInteger)
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column()
    embedding_version: Mapped[str] = mapped_column(Text, nullable=False)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    importance: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    last_accessed: Mapped[dt.datetime | None] = mapped_column()
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    supersedes: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")

    __table_args__ = (
        CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_memory_scope"
        ),
        CheckConstraint(
            "trust IN ('TRUSTED','DERIVED','UNTRUSTED_QUARANTINE')", name="ck_memory_trust"
        ),
        CheckConstraint(
            "status IN ('active','superseded','quarantined','retired')", name="ck_memory_status"
        ),
        CheckConstraint(
            "memory_type IN ('fact','episode','procedure','entity_ref')", name="ck_memory_type"
        ),
        CheckConstraint(
            "NOT (trust = 'UNTRUSTED_QUARANTINE' AND status = 'active')",
            name="ck_memory_quarantine_status",
        ),
        CheckConstraint("access_count >= 0", name="ck_memory_access_count"),
        Index(
            "ix_mem_scope",
            "organization_id",
            "scope",
            "scope_id",
            postgresql_where="status = 'active'",
        ),
        Index(
            "ix_mem_scope_key",
            "organization_id",
            "scope_key",
            postgresql_where="status = 'active'",
        ),
        Index(
            "ix_mem_quarantine_actor",
            "organization_id",
            "source_actor_id",
            postgresql_where="status = 'quarantined'",
        ),
        Index("ix_mem_source_run", "source_run_id"),
    )


class MemoryAudit(Base):
    """One row per thing that ever happened to a memory, in one vocabulary.

    The store's own four events (§3.1's ADD/UPDATE/DELETE/NOOP) and ours (ACCESS,
    QUARANTINE, RELEASE, PROMOTE, RETIRE, SUPERSEDE) share a table so that "what has
    happened to this memory" is a single ordered read rather than a union.
    """

    __tablename__ = "memory_audit"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    memory_id: Mapped[str] = mapped_column(Text, nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    actor_name: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    detail: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event IN ('ADD','UPDATE','DELETE','NOOP','ACCESS','QUARANTINE','RELEASE',"
            "'PROMOTE','RETIRE','SUPERSEDE')",
            name="ck_memory_audit_event",
        ),
        Index("ix_memory_audit_memory", "memory_id", "occurred_at"),
        Index("ix_memory_audit_org_time", "organization_id", "occurred_at"),
    )


class Entity(Base):
    """Relational, not vector (§2). "Which competitor did we write about in March" is a
    lookup; embedding it returns the four other companies in the same sentence."""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="department")
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    first_seen_run_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    merged_into: Mapped[uuid.UUID | None] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_entity_scope"
        ),
        Index(
            "uq_entity_canonical",
            "organization_id",
            "kind",
            "scope_id",
            text("lower(canonical_name)"),
            unique=True,
            postgresql_where="merged_into IS NULL",
        ),
        Index("ix_entity_org_kind", "organization_id", "kind"),
        Index("ix_entity_aliases", "aliases", postgresql_using="gin"),
    )


class EntityLink(Base):
    """No FK to `memory_metadata`: a link may point at a memory that was superseded, and
    a cascade would delete the evidence an audit needs."""

    __tablename__ = "entity_links"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    artifact_id: Mapped[uuid.UUID | None] = mapped_column()
    relation: Mapped[str] = mapped_column(Text, nullable=False, server_default="mentions")
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "memory_id IS NOT NULL OR run_id IS NOT NULL OR artifact_id IS NOT NULL",
            name="ck_entity_link_target",
        ),
        Index("ix_entity_link_entity", "entity_id"),
        Index(
            "uq_entity_link_memory",
            "entity_id",
            "memory_id",
            "relation",
            unique=True,
            postgresql_where="memory_id IS NOT NULL",
        ),
    )


class ScheduledIntention(Base):
    """ "Come back to this on Thursday", written where a machine can read it.

    Not a second scheduler: M1's `triggers` recur, an intention happens once. The
    existing scheduler drains this queue.
    """

    __tablename__ = "scheduled_intentions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="private")
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column()
    source_memory_id: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[uuid.UUID | None] = mapped_column()
    fired_run_id: Mapped[uuid.UUID | None] = mapped_column()
    fired_at: Mapped[dt.datetime | None] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','FIRED','CANCELLED','EXPIRED')", name="ck_intention_status"
        ),
        CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_intention_scope"
        ),
        Index(
            "uq_intention_pending",
            "organization_id",
            "actor_name",
            "dedupe_key",
            unique=True,
            postgresql_where="status = 'PENDING'",
        ),
        Index("ix_intention_due", "due_at", postgresql_where="status = 'PENDING'"),
        Index("ix_intention_actor", "organization_id", "actor_name"),
    )


class ProcedureCandidate(Base):
    """A proposal that some sequence of steps is worth remembering. Two observations is
    not a method; the counters are what make that sayable."""

    __tablename__ = "procedure_candidates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="private")
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    steps_hash: Mapped[str] = mapped_column(Text, nullable=False)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_run_id: Mapped[uuid.UUID | None] = mapped_column()
    last_run_id: Mapped[uuid.UUID | None] = mapped_column()
    last_failure_run_id: Mapped[uuid.UUID | None] = mapped_column()
    promoted_memory_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="OBSERVING")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('OBSERVING','PROPOSED','ADOPTED','REJECTED')", name="ck_procedure_status"
        ),
        CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_procedure_scope"
        ),
        CheckConstraint(
            "observed_count >= success_count + failure_count", name="ck_procedure_counts"
        ),
        CheckConstraint(
            "status <> 'ADOPTED' OR promoted_memory_id IS NOT NULL",
            name="ck_procedure_adopted_has_memory",
        ),
        UniqueConstraint("organization_id", "actor_name", "steps_hash", name="uq_procedure_shape"),
        Index(
            "ix_procedure_ready",
            "organization_id",
            "observed_count",
            postgresql_where="status = 'OBSERVING'",
        ),
    )


class MemoryPromotion(Base):
    """§8's queue. `ck_promotion_one_rung` and `ck_promotion_decided_has_reviewer` are
    the two constraints that make "no automatic promotion" a property of the database
    rather than of everyone's good intentions."""

    __tablename__ = "memory_promotions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    memory_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_scope: Mapped[str] = mapped_column(Text, nullable=False)
    to_scope: Mapped[str] = mapped_column(Text, nullable=False)
    to_scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    proposed_by: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_run_id: Mapped[uuid.UUID | None] = mapped_column()
    proposed_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PROPOSED")
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewer_kind: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    criteria: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column()
    promoted_memory_id: Mapped[str | None] = mapped_column(Text)
    review_cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PROPOSED','APPROVED','REJECTED','WITHDRAWN')", name="ck_promotion_status"
        ),
        CheckConstraint(
            "reviewer_kind IS NULL OR reviewer_kind IN ('model','human')",
            name="ck_promotion_reviewer_kind",
        ),
        CheckConstraint(
            "(from_scope = 'private' AND to_scope = 'department') OR "
            "(from_scope = 'department' AND to_scope = 'company')",
            name="ck_promotion_one_rung",
        ),
        CheckConstraint(
            "status IN ('PROPOSED','WITHDRAWN') OR "
            "(reviewer IS NOT NULL AND reviewer_kind IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_promotion_decided_has_reviewer",
        ),
        CheckConstraint(
            "status <> 'APPROVED' OR promoted_memory_id IS NOT NULL",
            name="ck_promotion_approved_has_result",
        ),
        Index(
            "uq_promotion_open",
            "organization_id",
            "memory_id",
            "to_scope",
            unique=True,
            postgresql_where="status = 'PROPOSED'",
        ),
        Index(
            "ix_promotion_queue",
            "organization_id",
            "proposed_at",
            postgresql_where="status = 'PROPOSED'",
        ),
        Index("ix_promotion_memory", "memory_id"),
    )


class ContextTrace(Base):
    """Why did this memory enter this prompt — and, in shadow mode, why it did not.

    `ck_trace_shadow_injects_nothing` is the row-level statement of §5's guarantee:
    shadow mode costs zero tokens and changes zero prompts. A shadow trace with a
    non-zero injected count means that guarantee broke somewhere.
    """

    __tablename__ = "context_traces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    node: Mapped[str] = mapped_column(Text, nullable=False)
    call_site: Mapped[str | None] = mapped_column(Text)
    query_text: Mapped[str | None] = mapped_column(Text)
    retrieved: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    retrieved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    injected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    injected_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    would_have_injected_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    embedding_version: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    shadow_mode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    grade: Mapped[str] = mapped_column(Text, nullable=False, server_default="ungraded")
    graded_by: Mapped[str | None] = mapped_column(Text)
    graded_at: Mapped[dt.datetime | None] = mapped_column()
    grade_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "grade IN ('helpful','neutral','harmful','ungraded')", name="ck_trace_grade"
        ),
        CheckConstraint("grade = 'ungraded' OR graded_by IS NOT NULL", name="ck_trace_graded_by"),
        CheckConstraint(
            "NOT shadow_mode OR (injected_count = 0 AND injected_tokens = 0)",
            name="ck_trace_shadow_injects_nothing",
        ),
        CheckConstraint("injected_count <= retrieved_count", name="ck_trace_injected_subset"),
        Index("ix_trace_run", "run_id"),
        Index("ix_trace_org_time", "organization_id", "created_at"),
        Index(
            "ix_trace_ungraded",
            "organization_id",
            "created_at",
            postgresql_where="grade = 'ungraded' AND retrieved_count > 0",
        ),
    )
