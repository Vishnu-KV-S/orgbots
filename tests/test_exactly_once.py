"""T7 — kill during a tool call. The exit criterion for M0.

The shape of every iteration:

1. Admit a run whose graph calls `fixture.sideeffect@1`.
2. Run it in a child process with `RUNTIME_CHAOS_CRASH_AFTER_EFFECT` set, so the
   tool `SIGKILL`s its own process in the window *after* the row is written and
   *before* the journal records the commit. That window is the whole problem.
3. Confirm the wreckage looks how it should: one fixture row, one `INTENT`.
4. Let the reaper requeue, let a second worker claim at a higher fence, let the
   graph replay the node.
5. Assert the tool did not fire again: still one fixture row, journal `COMMITTED`,
   run `SUCCESS`.

Run under both `sync` and `async` durability, because the host-dependence of
framework replay semantics is exactly the thing the journal exists to make
irrelevant — so it has to be proven, not assumed, under both.

`fixture.sideeffect` has a unique index on `marker`, so a genuine double-fire
raises `IntegrityError` inside the child rather than quietly appending a row.
Either way the count assertion catches it; the index catches it sooner.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import EffectStatus
from runtime.domain.ids import OrganizationId, RunId
from runtime.gateway.builtin.fixture_sideeffect import CRASH_AFTER_EFFECT_ENV
from runtime.settings import Settings
from runtime.worker.lease import Reaper
from tests.conftest_runtime import Runtime, build_runtime

pytestmark = [pytest.mark.integration, pytest.mark.chaos]

REPO_ROOT = Path(__file__).resolve().parents[1]

ITERATIONS = int(os.environ.get("RUNTIME_CHAOS_ITERATIONS", "50"))
"""The definition of done says 50. Lower it locally when iterating; CI runs the
full count on both a 16-core and a 1-vCPU runner."""


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


def _child_env(settings: Settings, *, crash: bool) -> dict[str, str]:
    env = {
        **os.environ,
        "RUNTIME_DATABASE_URL": settings.database_url,
        "RUNTIME_REDIS_URL": settings.redis_url,
        "RUNTIME_ARTIFACT_BACKEND": settings.artifact_backend,
        "RUNTIME_ARTIFACT_FS_ROOT": settings.artifact_fs_root,
        "RUNTIME_LEASE_SECONDS": str(settings.lease_seconds),
        "PYTHONPATH": str(REPO_ROOT),
    }
    if crash:
        env[CRASH_AFTER_EFFECT_ENV] = "1"
    else:
        env.pop(CRASH_AFTER_EFFECT_ENV, None)
    return env


async def _run_in_child(settings: Settings, run_id: RunId, *, crash: bool, durability: str) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.chaos_worker",
        str(run_id),
        durability,
        cwd=REPO_ROOT,
        env=_child_env(settings, crash=crash),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await process.communicate()
    assert process.returncode is not None
    if crash and process.returncode != -signal.SIGKILL:
        raise AssertionError(
            f"child was expected to be SIGKILLed, exited {process.returncode}: "
            f"{err.decode()[-2000:]}"
        )
    return process.returncode


async def _counts(rt: Runtime, run_id: RunId) -> tuple[int, str, str]:
    async with rt.uow() as uow:
        fixture_rows = (
            await uow.session.execute(
                text("SELECT count(*) FROM sideeffect_fixture WHERE run_id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
        effects = await uow.effects.for_run(run_id)
        run = await uow.runs.get(run_id)
    assert run is not None
    status = effects[0].status.value if effects else "NONE"
    return int(fixture_rows), status, run.status


async def _expire_and_reap(rt: Runtime, run_id: RunId) -> None:
    """Stand in for the passage of time.

    The killed worker's lease is still in the future; nothing recovers until it
    lapses. Moving the clock forward is what makes 50 iterations take seconds
    rather than minutes, and the lapse itself is still handled by the real reaper.
    """
    async with rt.uow.transaction() as uow:
        await uow.session.execute(
            text("UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )
    await Reaper(rt.uow, rt.settings).sweep()


@pytest.mark.parametrize("durability", ["sync", "async"])
async def test_kill_during_tool_call_executes_the_effect_exactly_once(
    rt: Runtime, durability: str
) -> None:
    """T7, once, with every intermediate state asserted."""
    started = await rt.start(
        "echo-agent", f"t7-{durability}", {"message": "x", "sideeffect": {"n": 1}}
    )
    await rt.relay.drain()

    await _run_in_child(rt.settings, started.run_id, crash=True, durability=durability)

    rows, effect_status, run_status = await _counts(rt, started.run_id)
    assert rows == 1, "the effect must have landed exactly once before the kill"
    assert effect_status == EffectStatus.INTENT.value, (
        "the journal must still say INTENT — that is the state recovery has to "
        "interpret, and if it says COMMITTED the kill happened in the wrong window"
    )
    assert run_status == "RUNNING"

    await _expire_and_reap(rt, started.run_id)

    outcome = await rt.worker.run_one(started.run_id)
    assert outcome is not None, "the recovering worker must be able to claim the run"

    rows, effect_status, run_status = await _counts(rt, started.run_id)
    assert rows == 1, "the tool fired a second time on replay"
    assert effect_status == EffectStatus.COMMITTED.value
    assert run_status == "SUCCESS"

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
    assert run is not None
    assert run.fence == 2, "the recovering worker must own a higher fence"


@pytest.mark.slow
@pytest.mark.parametrize("durability", ["sync", "async"])
async def test_kill_during_tool_call_repeated(rt: Runtime, durability: str) -> None:
    """T7 at volume. A race that only fires 2% of the time still fires."""
    for i in range(ITERATIONS):
        started = await rt.start(
            "echo-agent",
            f"t7-loop-{durability}-{i}",
            {"message": str(i), "sideeffect": {"n": i}},
        )
        await rt.relay.drain()
        await _run_in_child(rt.settings, started.run_id, crash=True, durability=durability)
        await _expire_and_reap(rt, started.run_id)
        assert await rt.worker.run_one(started.run_id) is not None

        rows, effect_status, run_status = await _counts(rt, started.run_id)
        assert (rows, effect_status, run_status) == (
            1,
            EffectStatus.COMMITTED.value,
            "SUCCESS",
        ), f"iteration {i} ({durability}) produced {rows} effect rows"


async def test_the_replayed_result_comes_from_the_probe_not_a_re_execution(
    rt: Runtime,
) -> None:
    """The distinction the whole design turns on.

    A run that recovers by re-running the tool would also end up with one row here
    — because the fixture's unique index would have raised. This asserts the
    intended path: the journal reports a replay, the probe finds the marker, and
    the tool is never called.
    """
    started = await rt.start("echo-agent", "t7-probe", {"sideeffect": {"probe": True}})
    await rt.relay.drain()
    await _run_in_child(rt.settings, started.run_id, crash=True, durability="sync")
    await _expire_and_reap(rt, started.run_id)
    await rt.worker.run_one(started.run_id)

    async with rt.uow() as uow:
        effects = await uow.effects.for_run(started.run_id)
        events = await uow.outbox.events_for_run(started.run_id)
        audit = await uow.audit.for_run(started.run_id)

    assert effects[0].attempts == 2, "the journal must have seen two attempts"
    assert effects[0].status is EffectStatus.COMMITTED
    assert events[-1]["payload"]["output"]["effect"]["replayed"] is True

    # Only one tool audit row, and it says "replayed". The killed attempt wrote
    # none: the kill lands between the effect and everything that records it.
    # That asymmetry is why the journal, not the audit log, is the durable record
    # of an attempt — the audit log describes decisions the runtime survived to
    # write down, and the whole point of T7 is the one it did not.
    assert [a["outcome"] for a in audit if a["action"].startswith("tool.")] == ["replayed"]


async def test_a_clean_run_in_a_child_process_still_works(rt: Runtime) -> None:
    """Control case. If this fails, the chaos harness is broken, not the runtime."""
    started = await rt.start("echo-agent", "t7-control", {"sideeffect": {}})
    await rt.relay.drain()
    assert await _run_in_child(rt.settings, started.run_id, crash=False, durability="sync") == 0

    rows, effect_status, run_status = await _counts(rt, started.run_id)
    assert (rows, effect_status, run_status) == (1, EffectStatus.COMMITTED.value, "SUCCESS")


async def test_the_real_reaper_recovers_a_killed_run_without_help(rt: Runtime) -> None:
    """The same recovery, driven only by the lease lapsing on its own.

    `_expire_and_reap` fast-forwards the clock everywhere else; this one waits, so
    that the fast-forward is never load-bearing for the claim that recovery works.
    """
    started = await rt.start("echo-agent", "t7-real-reaper", {"sideeffect": {}})
    await rt.relay.drain()
    await _run_in_child(rt.settings, started.run_id, crash=True, durability="sync")

    reaper = Reaper(rt.uow, rt.settings)
    deadline = asyncio.get_running_loop().time() + rt.settings.lease_seconds + 5
    while asyncio.get_running_loop().time() < deadline:
        if await reaper.sweep():
            break
        await asyncio.sleep(0.2)
    else:  # pragma: no cover - only on a stuck reaper
        pytest.fail("the reaper never requeued the killed run")

    assert await rt.worker.run_one(started.run_id) is not None
    rows, effect_status, run_status = await _counts(rt, started.run_id)
    assert (rows, effect_status, run_status) == (1, EffectStatus.COMMITTED.value, "SUCCESS")
