"""Migration 034 — the departments table, and the claim that it moves nothing.

M5 §3's exit criterion is precise: *"Test the migration against a database that has M3
memories in it, not an empty one."* An empty-database test would prove the DDL parses.
The thing that could actually go wrong is the re-point in step 4 — a foreign key
arriving over data that was written under a derived id — and it can only go wrong when
there is data.

So `test_034_backfills_over_live_m3_memories` downgrades to 033, seeds an organization
that looks like a running M3 department (actors placed by text, memories scoped by the
derived uuid, a RunSpec with the scope string pinned into it), and upgrades. Then it
asserts the two things a bad migration would break: the memories still point at their
department, and the pinned RunSpec still says what it said.

The rest of the module is the smaller checks — the two derivations, the sync trigger in
both directions, and the two places where a constant in Python has to agree with a
predicate in SQL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from runtime.budget.service import department_scope_id as budget_department_scope_id
from runtime.domain.enums import LIVE_RUN_STATUSES
from runtime.org.department import department_scope_id as memory_department_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from runtime.spec.validation import MAX_DELEGATION_DEPTH

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPARTMENT = "marketing"


def _alembic(args: list[str], settings: Settings) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "RUNTIME_DATABASE_URL": settings.database_url},
        check=True,
        capture_output=True,
    )


# --- the derivations ------------------------------------------------------------------


def test_the_migration_mirrors_the_two_scope_derivations() -> None:
    """Migration 034 spells out both `uuid5` derivations rather than importing them.

    That duplication is deliberate — a migration that imported application code would
    run whatever that code says *today*, not what it said when the migration was
    written, and a derivation that quietly changed would silently re-key every
    department in every database it had already run against.

    The check that the copies agree belongs in a test rather than in an import, and
    this is it. It loads the migration by path, because `034_departments` is not a
    legal module name and that is not an accident either.
    """
    import importlib.util

    path = (
        REPO_ROOT
        / "src"
        / "runtime"
        / "persistence"
        / "migrations"
        / "versions"
        / "034_departments.py"
    )
    spec = importlib.util.spec_from_file_location("m034", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    org = uuid.uuid4()
    for name in (DEPARTMENT, "research-ops", "a name with spaces"):
        assert module._budget_scope_id(org, name) == budget_department_scope_id(org, name)
        assert module._memory_scope_id(org, name) == memory_department_scope_id(org, name)


def test_the_two_derivations_are_distinct_and_stable() -> None:
    """Budget and memory chose different namespace strings in M2 and M3.

    034 carries **both** on the row rather than unifying them, because unifying them
    would mean rewriting either every `budget_pools.scope_id` or every
    `memory_metadata.scope_id` under a live organization — which is precisely the blast
    radius M4 declined and M5 §3 says to avoid.
    """
    org = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    budget = budget_department_scope_id(org, DEPARTMENT)
    memory = memory_department_scope_id(org, DEPARTMENT)
    assert budget != memory
    assert budget == budget_department_scope_id(org, DEPARTMENT), "stable"
    assert memory == memory_department_scope_id(org, DEPARTMENT), "stable"
    assert budget == uuid.uuid5(uuid.NAMESPACE_URL, f"department:{org}:{DEPARTMENT}")
    assert memory == uuid.uuid5(uuid.NAMESPACE_URL, f"memscope:department:{org}:{DEPARTMENT}")


# --- §3's exit criterion ------------------------------------------------------------------


async def test_034_backfills_over_live_m3_memories(settings: Settings) -> None:
    """The migration, run against a database that has M3 memories in it.

    Downgrades past 034, seeds a department the way M1-M4 would have left it — text on
    the actor row, memories under the derived scope id, a RunSpec with the scope string
    pinned — and upgrades. Four assertions, and each one is a way the re-point could
    have gone wrong:

    1. A `departments` row exists, backfilled from the text values.
    2. Its **primary key equals the budget scope id already in `budget_pools`**, so the
       foreign key arrived over data that already agreed.
    3. The M3 memory rows are untouched and still resolve to that department.
    4. The pinned RunSpec still says `dept:marketing`. §3: *"Dropping the text
       representation in the same migration that adds the FK breaks resumed runs."*
    """
    factory = UnitOfWorkFactory(settings)
    org = uuid.uuid4()
    memory_scope = memory_department_scope_id(org, DEPARTMENT)
    budget_scope = budget_department_scope_id(org, DEPARTMENT)
    memory_id = f"mem-{uuid.uuid4()}"

    _alembic(["downgrade", "033_identity"], settings)
    try:
        async with factory.transaction() as uow:
            await uow.session.execute(
                text("INSERT INTO organizations (id, name) VALUES (:id, 'm3-org')"), {"id": org}
            )
            await uow.session.execute(
                text(
                    """
                    INSERT INTO actors (id, organization_id, name, kind, department)
                    VALUES (:id, :org, 'research', 'llm_agent', :dept)
                    """
                ),
                {"id": uuid.uuid4(), "org": org, "dept": DEPARTMENT},
            )
            await uow.session.execute(
                text(
                    """
                    INSERT INTO roles (id, organization_id, name, department, rank)
                    VALUES (:id, :org, 'ic', :dept, 100)
                    """
                ),
                {"id": uuid.uuid4(), "org": org, "dept": DEPARTMENT},
            )
            # An M3 memory at department scope, written under the derived id.
            await uow.session.execute(
                text(
                    """
                    INSERT INTO memory_metadata (memory_id, organization_id, scope, scope_id,
                        scope_key, memory_type, trust, embedding_version, collection)
                    VALUES (:mid, :org, 'department', :sid, :skey, 'fact', 'TRUSTED',
                            'hashing-v1-256', 'mem_hashing_v1_256')
                    """
                ),
                {
                    "mid": memory_id,
                    "org": org,
                    "sid": memory_scope,
                    "skey": f"department:{memory_scope}",
                },
            )
            # A budget pool under the *other* derivation, which is what becomes the
            # department's primary key.
            await uow.session.execute(
                text(
                    """
                    INSERT INTO budget_pools (id, organization_id, scope_type, scope_id,
                        period, period_start, limit_cents)
                    VALUES (:id, :org, 'department', :sid, 'month', CURRENT_DATE, 60000)
                    """
                ),
                {"id": budget_scope, "org": org, "sid": budget_scope},
            )

        _alembic(["upgrade", "head"], settings)

        async with factory() as uow:
            row = (
                await uow.session.execute(
                    text(
                        "SELECT id, name, memory_scope_id, active FROM departments "
                        "WHERE organization_id = :org"
                    ),
                    {"org": org},
                )
            ).one()
            actor = (
                await uow.session.execute(
                    text(
                        "SELECT department, department_id FROM actors WHERE organization_id = :org"
                    ),
                    {"org": org},
                )
            ).one()
            memory = (
                await uow.session.execute(
                    text("SELECT scope_id, scope_key FROM memory_metadata WHERE memory_id = :m"),
                    {"m": memory_id},
                )
            ).one()
            pool_scope = (
                await uow.session.execute(
                    text(
                        "SELECT scope_id FROM budget_pools "
                        "WHERE organization_id = :org AND scope_type = 'department'"
                    ),
                    {"org": org},
                )
            ).scalar_one()

        assert row.name == DEPARTMENT, "backfilled from the text column"
        assert row.active is True
        assert row.id == budget_scope == pool_scope, (
            "the department's primary key must equal the budget scope id already in "
            "budget_pools — that identity is what makes the re-point move no rows"
        )
        assert row.memory_scope_id == memory_scope
        assert memory.scope_id == memory_scope, "the M3 memory row is untouched"
        assert memory.scope_key == f"department:{memory_scope}"
        assert actor.department == DEPARTMENT, "the text column survives until M6"
        assert actor.department_id == row.id, "and now points at the row"
    finally:
        _alembic(["upgrade", "head"], settings)


async def test_a_pinned_run_spec_still_resolves_after_034(settings: Settings) -> None:
    """§3's real hazard, stated as the thing it would break.

    `RunSpec.memory_scopes` and `memory_metadata.scope_id` are pinned into runs that
    are *already in flight* when the migration lands. This writes a run and its spec at
    033, migrates, and reads the spec back — a migration that dropped or rewrote the
    text representation would produce a spec that no longer says what it said, and the
    resumed run would read a different department's memories or none at all.
    """
    factory = UnitOfWorkFactory(settings)
    org, actor_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pinned = {"memory_scope": f"dept:{DEPARTMENT}", "department": DEPARTMENT}

    _alembic(["downgrade", "033_identity"], settings)
    try:
        async with factory.transaction() as uow:
            await uow.session.execute(
                text("INSERT INTO organizations (id, name) VALUES (:id, 'pinned')"), {"id": org}
            )
            await uow.session.execute(
                text(
                    "INSERT INTO actors (id, organization_id, name, kind, department) "
                    "VALUES (:id, :org, 'research', 'llm_agent', :dept)"
                ),
                {"id": actor_id, "org": org, "dept": DEPARTMENT},
            )
            version_id = (
                await uow.session.execute(
                    text(
                        "INSERT INTO actor_versions (actor_id, version, spec, spec_hash) "
                        "VALUES (:a, 1, '{}'::jsonb, 'h') RETURNING id"
                    ),
                    {"a": actor_id},
                )
            ).scalar_one()
            await uow.session.execute(
                text(
                    """
                    INSERT INTO runs (id, organization_id, root_run_id, actor_id,
                        actor_version_id, thread_id, status, idempotency_key)
                    VALUES (:id, :org, :id, :a, :v, :t, 'RUNNING', 'pinned-1')
                    """
                ),
                {"id": run_id, "org": org, "a": actor_id, "v": version_id, "t": str(run_id)},
            )
            await uow.session.execute(
                text(
                    "INSERT INTO run_specs (run_id, spec, spec_hash) "
                    "VALUES (:id, CAST(:spec AS jsonb), 'h')"
                ),
                {"id": run_id, "spec": json.dumps(pinned)},
            )

        _alembic(["upgrade", "head"], settings)

        async with factory() as uow:
            spec = (
                await uow.session.execute(
                    text("SELECT spec FROM run_specs WHERE run_id = :id"), {"id": run_id}
                )
            ).scalar_one()
            agent_path = (
                await uow.session.execute(
                    text("SELECT agent_path FROM runs WHERE id = :id"), {"id": run_id}
                )
            ).scalar_one()

        assert spec == pinned, "an in-flight run's spec is not rewritten by a migration"
        assert list(agent_path) == [], "and the new column defaults to empty, not NULL"
    finally:
        _alembic(["upgrade", "head"], settings)


# --- the sync trigger ----------------------------------------------------------------------


async def test_the_department_text_and_id_cannot_disagree(settings: Settings) -> None:
    """§3 step 5, in both directions.

    The id→text direction is the one that will matter in M6, when the id becomes what
    everything writes. A trigger that only handled text→id would have to be rewritten
    at exactly the moment the old column stopped being maintained, which is the worst
    possible time to be changing it.
    """
    factory = UnitOfWorkFactory(settings)
    org = uuid.uuid4()
    other = "research-ops"
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, 'sync')"), {"id": org}
        )
        for name in (DEPARTMENT, other):
            await uow.departments.ensure(
                budget_department_scope_id(org, name),
                org,
                name,
                memory_scope_id=memory_department_scope_id(org, name),
            )
        actor_id = uuid.uuid4()
        # Written the pre-M5 way: text only.
        await uow.session.execute(
            text(
                "INSERT INTO actors (id, organization_id, name, kind, department) "
                "VALUES (:id, :org, 'research', 'llm_agent', :dept)"
            ),
            {"id": actor_id, "org": org, "dept": DEPARTMENT},
        )

    async def read() -> tuple[str | None, uuid.UUID | None]:
        async with factory() as uow:
            row = (
                await uow.session.execute(
                    text("SELECT department, department_id FROM actors WHERE id = :id"),
                    {"id": actor_id},
                )
            ).one()
        return row.department, row.department_id

    assert await read() == (DEPARTMENT, budget_department_scope_id(org, DEPARTMENT))

    # text → id
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE actors SET department = :d WHERE id = :id"), {"d": other, "id": actor_id}
        )
    assert await read() == (other, budget_department_scope_id(org, other))

    # id → text
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE actors SET department_id = :d WHERE id = :id"),
            {"d": budget_department_scope_id(org, DEPARTMENT), "id": actor_id},
        )
    assert await read() == (DEPARTMENT, budget_department_scope_id(org, DEPARTMENT))

    # cleared
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE actors SET department = NULL WHERE id = :id"), {"id": actor_id}
        )
    assert await read() == (None, None)


async def test_an_unknown_department_name_leaves_the_id_null(settings: Settings) -> None:
    """The trigger syncs; it does not create.

    Creating rows from a trigger would mean a typo in a hand-written UPDATE mints a
    department, and departments are reviewed objects now — `apply` creates them from a
    `Department` document or they do not exist.
    """
    factory = UnitOfWorkFactory(settings)
    org, actor_id = uuid.uuid4(), uuid.uuid4()
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, 'nodept')"), {"id": org}
        )
        await uow.session.execute(
            text(
                "INSERT INTO actors (id, organization_id, name, kind, department) "
                "VALUES (:id, :org, 'research', 'llm_agent', 'typo-marketting')"
            ),
            {"id": actor_id, "org": org},
        )
    async with factory() as uow:
        row = (
            await uow.session.execute(
                text("SELECT department, department_id FROM actors WHERE id = :id"),
                {"id": actor_id},
            )
        ).one()
        count = (
            await uow.session.execute(
                text("SELECT count(*) FROM departments WHERE organization_id = :org"), {"org": org}
            )
        ).scalar_one()
    assert row.department == "typo-marketting"
    assert row.department_id is None
    assert count == 0, "a trigger must not mint a department"


async def test_a_department_is_deactivated_never_deleted(settings: Settings) -> None:
    """Edge case 78's rule, applied to departments.

    A hard delete would break a spend report's ability to name what money was spent on,
    and would orphan every memory written under the scope.
    """
    factory = UnitOfWorkFactory(settings)
    org = uuid.uuid4()
    async with factory.transaction() as uow:
        await uow.session.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, 'deact')"), {"id": org}
        )
        for name in (DEPARTMENT, "sunset"):
            await uow.departments.ensure(
                budget_department_scope_id(org, name),
                org,
                name,
                memory_scope_id=memory_department_scope_id(org, name),
            )
    async with factory.transaction() as uow:
        gone = await uow.departments.deactivate_missing(org, [DEPARTMENT])
    assert gone == ["sunset"]

    async with factory() as uow:
        rows = await uow.departments.all_for(org)
    assert {r.name: r.active for r in rows} == {DEPARTMENT: True, "sunset": False}


# --- two constants that have to agree with SQL ---------------------------------------------


async def test_the_live_index_predicate_matches_the_python_status_set(
    settings: Settings,
) -> None:
    """`LIVE_RUN_STATUSES` in Python; a partial index predicate in migration 035.

    A status added to one and not the other is a descendant the cascade would leave
    running, or an index that stops covering the query that needs it. Neither shows up
    as a failure anywhere else.
    """
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        predicate = (
            await uow.session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_runs_parent_live'")
            )
        ).scalar_one()
    for status in LIVE_RUN_STATUSES:
        assert f"'{status.value}'" in predicate, f"{status.value} is missing from the index"
    assert predicate.count("'") == 2 * len(LIVE_RUN_STATUSES), "and nothing else is in it"


def test_the_compiler_ceiling_matches_the_runtime_ceiling() -> None:
    """`spec.validation.MAX_DELEGATION_DEPTH` and `Settings.delegation_max_depth`.

    The compiler's copy is a literal because `spec validate` runs in CI's static job
    with no environment (M4's deviation table); the runtime's is a setting because an
    operator may need to lower it. They must start equal, or a document that validates
    in CI would be clamped at admission and describe something that does not happen.
    """
    assert Settings().delegation_max_depth == MAX_DELEGATION_DEPTH
