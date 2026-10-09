"""Shared fixtures.

Integration tests run against a real Postgres and a real Redis. There is no
in-memory substitute here on purpose: every property M0 is trying to prove —
`ON CONFLICT` semantics, `FOR UPDATE SKIP LOCKED`, CHECK constraints, consumer
group redelivery — is a property of the actual database and the actual broker.
A fake would prove that the fake works.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.ids import ActorId, OrganizationId
from runtime.persistence.engine import dispose_engines
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tables truncated between tests, in an order that respects foreign keys. Listed
# explicitly rather than discovered so that adding a table forces a decision about
# whether it is test state or fixture state.
_TRUNCATE_ORDER = [
    # M5 first, because `delegations` references `runs` twice — parent and child — and
    # `departments` is referenced by `actors`. Both would be reached by the
    # `organizations CASCADE` at the end, and both are listed anyway, because that is
    # the point of this list: adding a table forces a decision about whether it is test
    # state or fixture state. A delegation row is test state; so is a department, which
    # is a change from M4, where a department was a string on an actor and had no
    # lifetime of its own.
    "delegations",
    # Bots (037): conversations, rules and pending actions are test state, and all
    # three reference `bots`, so they come first. Routines (043) too: firings, then
    # the routines they belong to. Skills and recordings (044) are test state too.
    "bot_routine_runs",
    "bot_routines",
    "bot_recordings",
    "bot_skills",
    # Groups, wakes and reactions (047): wakes and reactions reference no organization.
    "bot_connectors",
    "bot_reactions",
    "bot_wakes",
    "bot_group_messages",
    "bot_group_members",
    "bot_groups",
    # Team file blobs (045) belong to no organization — they are keyed by their hash —
    # so `organizations CASCADE` never reaches them; truncating them reaches the files
    # that refer to them instead.
    "team_file_blobs",
    "bot_brief_revisions",
    "bot_memories",
    "bot_pending_actions",
    "bot_rules",
    "bot_messages",
    "bots",
    # M4: `apply_events` references `apply_plans`, and both reference
    # organizations. Listed explicitly even though the `organizations CASCADE` below
    # would reach them, because that is the point of this list — adding a table forces
    # a decision about whether it is test state or fixture state, and the config plane's
    # tables are test state.
    "apply_events",
    "apply_plans",
    "spec_documents",
    # M3 next: these reference organizations and, through `memory_id`, rows in the
    # `mem` schema that this list deliberately does not name. The vector collection
    # tables are created at runtime and named after an `embedding_version` (§3.3), so
    # they cannot be listed here — `_clean_memory_collections` truncates whatever
    # `mem.collections` says exists, which is the only enumeration that stays correct
    # when the embedder changes.
    "context_traces",
    "memory_promotions",
    "memory_audit",
    "memory_metadata",
    "entity_links",
    "entities",
    "scheduled_intentions",
    "procedure_candidates",
    # M2 next: audit_logs is partitioned, and truncating the parent cascades to
    # every partition — which is what we want and is also why it cannot be
    # discovered by listing pg_tables (that would name the partitions individually
    # and truncate the parent twice).
    "audit_logs",
    "credentials",
    "kill_switches",
    "rate_limit_policies",
    "approver_budgets",
    "budget_allocations",
    "tool_grants",
    "connections",
    "authority_policies",
    "roles",
    # M1 next: these reference tasks, which reference goals/projects.
    "task_evaluations",
    "inbox_messages",
    "session_summaries",
    "sessions",
    "trigger_fires",
    "triggers",
    "approvals",
    "tasks",
    "projects",
    "goals",
    "usage_ledger",
    "budget_reservations",
    "budget_pools",
    "artifact_links",
    "artifact_versions",
    "artifacts",
    "audit_log",
    "sideeffect_fixture",
    "effect_intents",
    "events",
    "outbox",
    "run_specs",
    "runs",
]


def _database_url() -> str:
    return os.environ.get(
        "RUNTIME_DATABASE_URL",
        "postgresql+psycopg://runtime@127.0.0.1:54329/runtime",
    )


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    root = tmp_path_factory.mktemp("artifacts")
    return Settings(
        database_url=_database_url(),
        redis_url=os.environ.get("RUNTIME_REDIS_URL", "redis://127.0.0.1:63799/0"),
        artifact_backend="fs",
        artifact_fs_root=str(root),
        lease_seconds=5.0,
        heartbeat_seconds=1.0,
        relay_interval_seconds=0.05,
        log_json=False,
    )


SESSION_LOCK_KEY = 0x4D30_7E57
"""Advisory lock id for "a test session owns this database"."""


@pytest.fixture(scope="session", autouse=True)
def _migrated(settings: Settings) -> Iterator[None]:
    """Bring the schema to head once per session, and take exclusive ownership of
    the database for the duration.

    The exclusivity is not fussiness. `_clean_tables` truncates every table before
    every test, so two pytest sessions pointed at one database delete each other's
    rows mid-test. The failures that produces are spectacular and completely
    misleading — they look like lost runs and double-fired effects, which is to
    say they look exactly like the bugs this suite exists to detect.

    A session-scoped `pg_advisory_lock` turns that into a wait: the second session
    blocks here until the first finishes, rather than corrupting it.
    """
    import psycopg

    dsn = settings.database_url.replace("postgresql+psycopg", "postgresql")
    with psycopg.connect(dsn, autocommit=True) as guard:
        # The lock is taken *before* migrating, not after. Two `alembic upgrade
        # head` runs against one database is its own race, and T0 downgrades to
        # base — which would drop the tables another session is mid-test on.
        held = guard.execute("SELECT pg_try_advisory_lock(%s)", (SESSION_LOCK_KEY,)).fetchone()
        if not (held and held[0]):
            # Printed before pytest starts capturing, so the wait is visible.
            print(
                f"\nwaiting for another test session to release {dsn.rsplit('/', 1)[-1]}...",
                flush=True,
            )
            guard.execute("SELECT pg_advisory_lock(%s)", (SESSION_LOCK_KEY,))

        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPO_ROOT,
                env={**os.environ, "RUNTIME_DATABASE_URL": settings.database_url},
                check=True,
                capture_output=True,
            )
            yield
        finally:
            guard.execute("SELECT pg_advisory_unlock(%s)", (SESSION_LOCK_KEY,))


@pytest_asyncio.fixture
async def uow_factory(settings: Settings) -> AsyncIterator[UnitOfWorkFactory]:
    factory = UnitOfWorkFactory(settings)
    yield factory


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(settings: Settings) -> AsyncIterator[None]:
    """Truncate between tests.

    Runs *before* each test rather than after, so a failed test leaves its rows
    behind for inspection.
    """
    factory = UnitOfWorkFactory(settings)
    async with factory.transaction() as uow:
        await uow.session.execute(
            text(f"TRUNCATE {', '.join(_TRUNCATE_ORDER)} RESTART IDENTITY CASCADE")
        )
        # M3. The vector collections are created at runtime, so they are discovered
        # rather than listed. `mem.collections` itself is *kept*: dropping the registry
        # between tests would make every `NativeMemoryStore.setup()` re-create a table
        # that already exists, and would lose the dimension T45 depends on.
        collections = (
            await uow.session.execute(text("SELECT name FROM mem.collections"))
        ).scalars()
        for name in collections:
            await uow.session.execute(text(f'TRUNCATE mem."{name}"'))
        await uow.session.execute(text("UPDATE actors SET active_version_id = NULL WHERE true"))
        await uow.session.execute(text("TRUNCATE actor_versions, actors CASCADE"))
        # M5. After `actors`, because `actors.department_id` references it — and before
        # `organizations`, whose CASCADE would reach it anyway. Explicit for the reason
        # in the comment above `_TRUNCATE_ORDER`.
        await uow.session.execute(text("TRUNCATE departments CASCADE"))
        await uow.session.execute(text("TRUNCATE organizations CASCADE"))
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose() -> AsyncIterator[None]:
    yield
    await dispose_engines()


@pytest.fixture
def organization_id() -> OrganizationId:
    return OrganizationId(uuid.UUID("00000000-0000-0000-0000-0000000000aa"))


@pytest.fixture
def actor_id() -> ActorId:
    return ActorId(uuid.UUID("00000000-0000-0000-0000-0000000000bb"))
