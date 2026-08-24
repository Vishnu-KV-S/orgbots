"""Kill the applier mid-apply; assert no partial state.

§12's hard exit criterion, and the one that cannot be written with a `raise`. See
`tests/chaos_applier.py` for why the kill happens inside the child process.

**The environment has to be exported.** The child reads `Settings()` from `os.environ`,
not from the pytest fixture, so a child launched without `RUNTIME_DATABASE_URL` would
cheerfully connect to a different database and pass.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.chaos_applier import KILL_AFTER_ENV, PHASES

pytestmark = [pytest.mark.integration, pytest.mark.chaos]

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = "config/org"


def _child_env(settings: Settings, *, kill_after: str | None) -> dict[str, str]:
    env = {
        **os.environ,
        "RUNTIME_DATABASE_URL": settings.database_url,
        "RUNTIME_REDIS_URL": settings.redis_url,
        "RUNTIME_ARTIFACT_BACKEND": settings.artifact_backend,
        "RUNTIME_ARTIFACT_FS_ROOT": settings.artifact_fs_root,
        "PYTHONPATH": str(REPO_ROOT),
    }
    if kill_after:
        env[KILL_AFTER_ENV] = kill_after
    else:
        env.pop(KILL_AFTER_ENV, None)
    return env


async def _apply_in_child(
    settings: Settings, organization_id: OrganizationId, *, kill_after: str | None
) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.chaos_applier",
        str(organization_id),
        SPEC_PATH,
        cwd=REPO_ROOT,
        env=_child_env(settings, kill_after=kill_after),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await process.communicate()
    assert process.returncode is not None
    if kill_after and process.returncode != -signal.SIGKILL:
        raise AssertionError(
            f"child was expected to be SIGKILLed, exited {process.returncode}: "
            f"{err.decode()[-2000:]}"
        )
    return process.returncode


async def _rows(uow_factory: UnitOfWorkFactory, organization_id: OrganizationId) -> dict[str, int]:
    """Every table an apply writes, counted. Nothing may be non-zero after a kill."""
    tables = {
        "actors": "organization_id",
        "roles": "organization_id",
        "connections": "organization_id",
        "tool_grants": "organization_id",
        "authority_policies": "organization_id",
        "triggers": "organization_id",
        "spec_documents": "organization_id",
        "apply_plans": "organization_id",
        "apply_events": "organization_id",
    }
    out: dict[str, int] = {}
    async with uow_factory() as uow:
        for table, column in tables.items():
            out[table] = int(
                (
                    await uow.session.execute(
                        text(f"SELECT count(*) FROM {table} WHERE {column} = :org"),
                        {"org": organization_id},
                    )
                ).scalar_one()
            )
        out["actor_versions"] = int(
            (
                await uow.session.execute(
                    text(
                        """
                        SELECT count(*) FROM actor_versions av
                          JOIN actors a ON a.id = av.actor_id
                         WHERE a.organization_id = :org
                        """
                    ),
                    {"org": organization_id},
                )
            ).scalar_one()
        )
    return out


@pytest.mark.parametrize("phase", PHASES)
async def test_a_killed_apply_leaves_nothing(
    settings: Settings, uow_factory: UnitOfWorkFactory, phase: str
) -> None:
    """Edge case 74, proved the hard way.

    The child dies with rows written and uncommitted. Postgres aborts the transaction
    when the backend goes away, so the correct outcome is *nothing* — not a partial
    organization, not an actor with no version, not a version with no active pointer.
    """
    organization_id = OrganizationId(uuid.uuid4())

    await _apply_in_child(settings, organization_id, kill_after=phase)

    counts = await _rows(uow_factory, organization_id)
    assert counts == dict.fromkeys(counts, 0), (
        f"killing after {phase} left partial state: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v)
    )

    async with uow_factory() as uow:
        organization = (
            await uow.session.execute(
                text("SELECT count(*) FROM organizations WHERE id = :org"),
                {"org": organization_id},
            )
        ).scalar_one()
    assert organization == 0


async def test_the_control_case_applies(settings: Settings, uow_factory: UnitOfWorkFactory) -> None:
    """If this fails, the chaos harness is broken rather than the runtime."""
    organization_id = OrganizationId(uuid.uuid4())
    assert await _apply_in_child(settings, organization_id, kill_after=None) == 0
    counts = await _rows(uow_factory, organization_id)
    assert counts["actors"] == 4
    assert counts["actor_versions"] == 4
    assert counts["triggers"] == 4


async def test_a_retry_after_a_kill_succeeds(
    settings: Settings, uow_factory: UnitOfWorkFactory
) -> None:
    """The advisory lock is transaction-scoped, so a killed applier cannot leave an
    organization locked (edge case 75). The retry is the proof."""
    organization_id = OrganizationId(uuid.uuid4())
    await _apply_in_child(settings, organization_id, kill_after="references")
    assert await _apply_in_child(settings, organization_id, kill_after=None) == 0
    counts = await _rows(uow_factory, organization_id)
    assert counts["actors"] == 4
