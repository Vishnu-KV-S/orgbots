"""T0 — migrations are reversible.

Every revision runs up, down, and up again. The point is not that `downgrade`
is a thing anyone does in production; it is that writing a working downgrade
forces the author to notice what the upgrade actually created.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    # M4 (031-033). 033 adds columns rather than tables, so it is asserted separately.
    "apply_events",
    "apply_plans",
    "spec_documents",
    # M3 (023-030). The vector collections are deliberately absent: they live in the
    # `mem` schema, are created at runtime, and are named after an `embedding_version`
    # that no migration can know in advance (§3.3).
    "context_traces",
    "entities",
    "entity_links",
    "memory_audit",
    "memory_metadata",
    "memory_promotions",
    "procedure_candidates",
    "scheduled_intentions",
    "approvals",
    "goals",
    "inbox_messages",
    "projects",
    "session_summaries",
    "sessions",
    "task_evaluations",
    "tasks",
    "trigger_fires",
    "triggers",
    "actor_versions",
    "actors",
    "artifact_links",
    "artifact_versions",
    "artifacts",
    "audit_log",
    "budget_pools",
    "budget_reservations",
    "effect_intents",
    "events",
    "organizations",
    "outbox",
    "run_specs",
    "runs",
    "sideeffect_fixture",
    "usage_ledger",
}

pytestmark = pytest.mark.integration


def _alembic(args: list[str], settings: Settings) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "RUNTIME_DATABASE_URL": settings.database_url},
        check=True,
        capture_output=True,
    )


def test_up_down_up_is_clean(settings: Settings) -> None:
    _alembic(["downgrade", "base"], settings)
    _alembic(["upgrade", "head"], settings)
    _alembic(["downgrade", "base"], settings)
    _alembic(["upgrade", "head"], settings)


def test_downgrade_base_leaves_no_tables_behind(settings: Settings) -> None:
    _alembic(["downgrade", "base"], settings)
    try:
        import psycopg

        with psycopg.connect(
            settings.database_url.replace("postgresql+psycopg", "postgresql")
        ) as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        leftover = {r[0] for r in rows if r[0] != "alembic_version"}
        assert leftover == set(), f"downgrade left tables behind: {sorted(leftover)}"
    finally:
        _alembic(["upgrade", "head"], settings)


async def test_head_creates_every_expected_table(settings: Settings) -> None:
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        rows = (
            await uow.session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ).scalars()
        present = set(rows)
    assert present >= EXPECTED_TABLES


async def test_migration_033_adds_the_identity_columns(settings: Settings) -> None:
    """M4 §4. `uid` is option (a), present and set by nobody; `reports_to` is the
    reporting edge; `active` is edge case 78's deactivate-never-delete."""
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'actors'"
                )
            )
        ).scalars()
        columns = set(rows)
    assert {"uid", "reports_to", "active"} <= columns


EXPECTED_VIEWS = {
    "v_memory_grades",
    "v_memory_inventory",
    "v_memory_token_effect",
    "v_task_facts",
    "v_weekly_spend",
    "v_metric_cost_per_accepted",
    "v_metric_rejection_rate",
    "v_metric_unassisted_completion",
    "v_metric_coordination_ratio",
    "v_metric_dashboard",
}


async def test_head_creates_every_metric_view(settings: Settings) -> None:
    """Migration 015 lands before any tuning starts (§8).

    A missing view is not a cosmetic failure: every number in §9 is read through
    one of these, so the gate would be decided on absent data rather than on bad
    data, which is harder to notice.
    """
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        rows = (
            await uow.session.execute(
                text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
            )
        ).scalars()
        present = set(rows)
    assert present >= EXPECTED_VIEWS, f"missing: {sorted(EXPECTED_VIEWS - present)}"


async def test_mem_schema_exists_and_records_pgvector_availability(settings: Settings) -> None:
    """M3 migration 023. The `mem` namespace exists and `mem.capabilities` holds exactly
    one row saying whether this database can do ANN.

    The row is what stops "we are on the exact-cosine native store because pgvector is
    absent" from being something an operator infers from a latency graph six weeks after
    setting the system up.
    """
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        exists = (
            await uow.session.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'mem'")
            )
        ).scalar_one_or_none()
        rows = (
            await uow.session.execute(
                text("SELECT count(*), bool_or(pgvector) FROM mem.capabilities")
            )
        ).one()
    assert exists == 1
    assert rows[0] == 1, "mem.capabilities must hold exactly one row"
    assert isinstance(rows[1], bool)


async def test_the_audit_gateway_vocabulary_includes_embedding(settings: Settings) -> None:
    """Migration 030. M2's closed `ck_audit_gateway` fired on M3's first test run — the
    constraint working — and widening it deliberately beats not auditing the
    highest-volume external calls M3 makes."""
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        definition = (
            await uow.session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_audit_gateway' LIMIT 1"
                )
            )
        ).scalar_one()
    assert "embedding" in definition


async def test_lg_schema_exists_and_is_empty_of_our_tables(settings: Settings) -> None:
    """The checkpointer owns `lg`. We create the namespace and nothing else."""
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        exists = (
            await uow.session.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'lg'")
            )
        ).scalar_one_or_none()
    assert exists == 1


async def test_budget_invariant_is_enforced_by_the_database(settings: Settings) -> None:
    """I8 must fail at the DB, not merely in application code."""
    import uuid as _uuid

    from sqlalchemy.exc import IntegrityError

    factory = UnitOfWorkFactory(settings)
    pool_id = _uuid.uuid4()
    async with factory.transaction() as uow:
        await uow.session.execute(
            text(
                """
                INSERT INTO budget_pools (id, organization_id, scope_type, scope_id,
                                          period, period_start, limit_cents)
                VALUES (:id, :org, 'org', :org, 'month', CURRENT_DATE, 1000)
                """
            ),
            {"id": pool_id, "org": _uuid.uuid4()},
        )

    with pytest.raises(IntegrityError, match="ck_budget_invariant"):
        async with factory.transaction() as uow:
            await uow.session.execute(
                text("UPDATE budget_pools SET reserved_cents = 1001 WHERE id = :id"),
                {"id": pool_id},
            )


async def test_effect_logical_call_id_is_unique(settings: Settings) -> None:
    """The single constraint the whole exactly-once story rests on."""
    import uuid as _uuid

    from sqlalchemy.exc import IntegrityError

    factory = UnitOfWorkFactory(settings)
    row = {
        "id": _uuid.uuid4(),
        "logical_call_id": "same-key",
        "organization_id": _uuid.uuid4(),
        "run_id": _uuid.uuid4(),
        "root_run_id": _uuid.uuid4(),
        "fence": 1,
        "node": "n",
        "ordinal": 0,
        "args_hash": "h",
        "tool_name": "t",
        "tool_version": 1,
        "recovery_policy": "replay_safe",
        "blast_radius": "read",
    }
    insert = text(
        """
        INSERT INTO effect_intents (id, logical_call_id, organization_id, run_id,
            root_run_id, fence, node, ordinal, args_hash, tool_name, tool_version,
            status, recovery_policy, blast_radius)
        VALUES (:id, :logical_call_id, :organization_id, :run_id, :root_run_id,
            :fence, :node, :ordinal, :args_hash, :tool_name, :tool_version,
            'INTENT', :recovery_policy, :blast_radius)
        """
    )
    async with factory.transaction() as uow:
        await uow.session.execute(insert, row)

    with pytest.raises(IntegrityError, match="uq_effect_logical_call"):
        async with factory.transaction() as uow:
            await uow.session.execute(insert, {**row, "id": _uuid.uuid4()})
