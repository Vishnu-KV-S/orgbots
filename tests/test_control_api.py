"""`/v1/control` — plan, apply, stop, start, tick.

The file surface has its own module. This one is about the verbs that change the
database, and every test here is a way the control surface could be *worse* than the
CLI it is standing in for rather than merely different:

- an apply that did not go through the compiler would be a second definition of what a
  document means;
- a stale plan that applied anyway would apply a diff nobody reviewed (§13 risk 1);
- clicking *apply* twice would be a 500 instead of a conflict, and the operator would
  not know whether the first one worked;
- a stop that only paused the triggers would leave every in-flight run publishing;
- a start that did not resume the triggers would leave a department that reads as
  running and never wakes.

The fixture applies `config/testco/` into a temporary root, because it is a real
company: an organization, a department, four actors, roles, grants, budgets and a
schedule. A hand-built two-document corpus would pass these tests while the shipped
configuration was broken.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.api.app import create_app
from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FOLDER = "config/testco"
DEPARTMENT = "growth"
HEAD = "marketing-head"


@pytest.fixture
def spec_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`config/testco/` copied somewhere a test may edit it.

    A copy rather than the repository's own folder: these tests apply, stop and start,
    and one that wrote to `config/` would leave the developer's checkout dirty in a way
    that looks like their own work.
    """
    root = tmp_path / "config"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config" / "testco", root / "testco")
    monkeypatch.chdir(tmp_path)
    return root


@pytest_asyncio.fixture
async def client(
    settings: Settings, uow_factory: UnitOfWorkFactory, spec_root: Path
) -> AsyncIterator[httpx.AsyncClient]:
    scoped = settings.model_copy(update={"spec_roots": "config"})
    app = create_app(scoped)
    app.state.settings = scoped
    app.state.uow = uow_factory
    from runtime.runtime.run_service import RunService

    app.state.service = RunService(uow_factory, settings=scoped)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def org() -> OrganizationId:
    return OrganizationId(uuid.uuid4())


async def _apply(client: httpx.AsyncClient, org: OrganizationId, **extra: object) -> dict:
    """validate → plan → apply, the flow the UI walks a person through."""
    validated = await client.post("/v1/control/specs/validate", json={"path": FOLDER})
    assert validated.status_code == 200, validated.text

    planned = await client.post(
        f"/v1/control/organizations/{org}/plan", json={"path": FOLDER, **extra}
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()

    applied = await client.post(
        f"/v1/control/organizations/{org}/apply",
        json={"path": FOLDER, "plan_id": plan["plan_id"], **extra},
    )
    assert applied.status_code == 200, applied.text
    return {"plan": plan, "applied": applied.json()}


# --- validate, plan, apply ------------------------------------------------------------


async def test_validate_reports_the_company_and_touches_no_database(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.post("/v1/control/specs/validate", json={"path": FOLDER})).json()
    assert body["ok"] is True
    assert body["organization"] == "testco"
    assert HEAD in [a["name"] for a in body["actors"]]
    assert body["counts"]["triggers"] > 0


async def test_validate_accepts_every_entrypoint_the_worker_can_run(
    client: httpx.AsyncClient,
) -> None:
    """The registry gap, asserted from the outside.

    `default_registries()` used to import only `runtime.graphs.department` and
    `runtime.handlers`, so a document naming `echo_agent@1` or `delegator@1` — both
    perfectly runnable — failed validation. CI red for a spec that would have applied
    and run.
    """
    from runtime.spec.validation import default_registries

    graphs = default_registries().graphs
    assert {"echo_agent@1", "delegator@1", "marketing_head@1"} <= graphs


async def test_the_plan_carries_its_hash_and_its_expiry(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """Fifteen minutes (`STALE_AFTER_SECONDS`) is long enough to read a diff and short
    enough that the diff is still the diff. The UI can only show a countdown if the
    window is on the wire, and discovering the expiry as a 409 after reading the diff
    is the worst version of this."""
    body = (
        await client.post(f"/v1/control/organizations/{org}/plan", json={"path": FOLDER})
    ).json()
    assert body["plan_id"] is not None
    assert body["plan_hash"] == body["render"].rsplit("plan_hash: ", 1)[1].strip()
    assert body["empty"] is False
    assert body["expires_in_seconds"] == 15 * 60
    assert body["expires_at"] is not None
    assert "plan_hash:" in body["render"]


async def test_apply_creates_the_company_and_the_viewer_can_see_it(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """The end of the flow: the canvas shows a company that did not exist before."""
    result = await _apply(client, org)
    assert result["applied"]["applied"] is True
    assert result["applied"]["created"] > 0

    listed = (await client.get("/v1/observe/organizations")).json()["organizations"]
    assert [o["name"] for o in listed] == ["testco"]
    assert listed[0]["departments"] == 1
    assert listed[0]["actors"] == 4

    graph = (await client.get(f"/v1/observe/organizations/{org}/graph")).json()
    departments = [n for n in graph["nodes"] if n["type"] == "department"]
    assert [d["label"] for d in departments] == [DEPARTMENT]
    assert departments[0]["data"]["members"] == 4
    assert departments[0]["data"]["state"] == "running"
    assert departments[0]["data"]["kill_switch"] is None
    assert departments[0]["data"]["triggers"], "the schedule is on the department node"

    head = next(n for n in graph["nodes"] if n["label"] == HEAD)
    assert "weekly_plan" in head["data"]["modes"], "the graph's own branch, not the schedule"
    assert "weekly_metrics" not in head["data"]["modes"]


async def test_a_stale_plan_is_409_and_not_applied(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """Edge case 76. Somebody applied something between the plan and the apply, so the
    diff that was read is not the diff that would be applied."""
    planned = (
        await client.post(f"/v1/control/organizations/{org}/plan", json={"path": FOLDER})
    ).json()

    # Somebody else applies in the meantime — an unplanned apply, like a script.
    moved = await client.post(f"/v1/control/organizations/{org}/apply", json={"path": FOLDER})
    assert moved.status_code == 200

    response = await client.post(
        f"/v1/control/organizations/{org}/apply",
        json={"path": FOLDER, "plan_id": planned["plan_id"]},
    )
    assert response.status_code == 409
    assert "moved since this plan was made" in response.json()["detail"]


async def test_applying_the_same_plan_twice_is_a_clean_409(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """Clicking the button twice. A 500 here would leave the operator unable to tell
    whether the first one worked, which is the worst possible answer."""
    result = await _apply(client, org)
    plan_id = result["plan"]["plan_id"]

    again = await client.post(
        f"/v1/control/organizations/{org}/apply", json={"path": FOLDER, "plan_id": plan_id}
    )
    assert again.status_code == 409
    assert "is APPLIED, not PENDING" in again.json()["detail"]


async def test_an_unknown_plan_is_404_and_a_bad_folder_is_404(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    missing_plan = await client.post(
        f"/v1/control/organizations/{org}/apply",
        json={"path": FOLDER, "plan_id": str(uuid.uuid4())},
    )
    assert missing_plan.status_code == 404

    missing_folder = await client.post("/v1/control/specs/validate", json={"path": "config/nope"})
    assert missing_folder.status_code == 404


async def test_history_and_drift_are_reachable_without_a_shell(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """The whole mitigation for "an apply cannot be undone" is that it can be read.

    A record only the CLI could reach was a mitigation only somebody with a shell had.
    """
    await _apply(client, org)

    history = (await client.get(f"/v1/control/organizations/{org}/history")).json()
    assert history["events"], "an apply leaves a record"
    assert {e["action"] for e in history["events"]} <= {"create", "update", "deactivate", "rename"}

    drift = (
        await client.post(f"/v1/control/organizations/{org}/drift", json={"path": FOLDER})
    ).json()
    assert drift["clean"] is True, drift["render"]


# --- departments ----------------------------------------------------------------------


async def test_stop_engages_the_switch_and_pauses_the_triggers(
    client: httpx.AsyncClient, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """Two mechanisms, because neither alone is a stop.

    Pausing the triggers stops new work being *scheduled*; the kill switch stops runs
    and tool calls that are already in flight. And the switch is the authoritative half:
    `TriggerRepository.upsert` forces `active = true`, so the next apply resumes the
    triggers and does not touch the switch.
    """
    await _apply(client, org)

    stopped = await client.post(
        f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop",
        json={"mode": "drain", "reason": "spending too fast"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["triggers_paused"] > 0

    body = (await client.get(f"/v1/control/organizations/{org}/departments")).json()
    department = body["departments"][0]
    assert department["state"] == "stopped"
    assert department["kill_switch"]["mode"] == "drain"
    assert department["kill_switch"]["reason"] == "spending too fast"
    assert not any(t["active"] for t in department["triggers"])

    # And a tick now starts nothing, because there is no active trigger to fire.
    ticked = (await client.post(f"/v1/control/organizations/{org}/tick")).json()
    assert ticked["fired"] == []

    # The run admission check refuses too — that is the half a trigger pause cannot do.
    from runtime.org.killswitch import KillSwitchService

    verdict = await KillSwitchService(uow_factory).admits_runs(org, HEAD)
    assert verdict.stopped and verdict.scope == f"department:{DEPARTMENT}"


async def test_a_second_stop_is_409_rather_than_a_silent_mode_change(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """Two live switches for one scope in different modes is an ambiguity nobody
    resolves correctly at 3am, so the second caller is told no."""
    await _apply(client, org)
    first = await client.post(
        f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop", json={}
    )
    assert first.status_code == 200

    second = await client.post(
        f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop",
        json={"mode": "halt"},
    )
    assert second.status_code == 409
    assert "already live" in second.json()["detail"]


async def test_start_recovers_both_halves(client: httpx.AsyncClient, org: OrganizationId) -> None:
    await _apply(client, org)
    await client.post(f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop", json={})

    started = await client.post(
        f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/start", json={}
    )
    assert started.status_code == 200
    assert started.json()["kill_switch_disengaged"] is True
    assert started.json()["triggers_resumed"] > 0

    department = (await client.get(f"/v1/control/organizations/{org}/departments")).json()[
        "departments"
    ][0]
    assert department["state"] == "running"
    assert department["kill_switch"] is None


async def test_start_then_run_is_admitted_immediately_in_the_same_process(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """Pressing *start* and then *run* must work, with no ten-second wait.

    The kill-switch cache is per **instance**. When the control surface built its own
    `KillSwitchService`, `start`'s `invalidate()` cleared a cache that nothing read, and
    admission kept refusing from a stale copy for the full TTL — with the switch already
    disengaged in the database. Ten seconds is the *cross-process* propagation this
    surface documents; the process that pulled the switch must not impose it on itself.
    """
    await _apply(client, org)
    await client.post(f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop", json={})

    refused = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run", json={"mode": "weekly_plan"}
    )
    assert refused.json()["admitted"] is False

    await client.post(f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/start", json={})

    admitted = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run",
        json={"mode": "weekly_plan", "idempotency_key": f"after-start:{uuid.uuid4()}"},
    )
    body = admitted.json()
    assert body["admitted"] is True, body.get("refusal_reason")
    assert body["status"] == "QUEUED"


async def test_starting_an_already_running_department_is_not_an_error(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """*Start* is the idempotent direction: the caller asked for a running department
    and there is one. Refusing would make the button lie about what it did."""
    await _apply(client, org)
    response = await client.post(
        f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/start", json={}
    )
    assert response.status_code == 200
    assert response.json()["kill_switch_disengaged"] is False


async def test_an_unknown_department_is_404(client: httpx.AsyncClient, org: OrganizationId) -> None:
    await _apply(client, org)
    for verb in ("stop", "start"):
        response = await client.post(
            f"/v1/control/organizations/{org}/departments/nowhere/{verb}", json={}
        )
        assert response.status_code == 404


# --- runs -----------------------------------------------------------------------------


async def test_starting_a_run_goes_through_run_service(
    client: httpx.AsyncClient, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """202, and the run is queued rather than executed. The API never runs a graph."""
    await _apply(client, org)
    response = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run",
        json={"mode": "weekly_plan", "input": {}},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    # QUEUED *as returned*, not as read back. The run's later status is not this
    # endpoint's business and asserting on it would make the test a race against any
    # worker that happens to be running — including the one a developer left up.
    assert body["status"] == "QUEUED"
    assert body["spec_hash"], "the run was admitted against a frozen spec"

    async with uow_factory() as uow:
        exists = (
            await uow.session.execute(
                text("SELECT count(*) FROM run_specs WHERE run_id = :id"),
                {"id": body["run_id"]},
            )
        ).scalar_one()
    assert exists == 1, "the spec was frozen at admission, not at execution"


async def test_the_same_idempotency_key_twice_starts_one_run(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """A nervous operator pressing the button twice, and a retry, are the same request.

    Omitting the key mints a fresh one, so two deliberate clicks are still two runs —
    which is why the key is optional rather than derived.
    """
    await _apply(client, org)
    key = f"ui:{uuid.uuid4()}"
    first = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run",
        json={"mode": "weekly_plan", "idempotency_key": key},
    )
    second = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run",
        json={"mode": "weekly_plan", "idempotency_key": key},
    )
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False


async def test_a_refused_run_is_a_row_not_an_error(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """A kill switch does not raise here, and the response must not pretend it did.

    `RunService` records a refusal as a `LIMIT_REACHED` run with a reason, because a
    refusal that left no trace is one nobody can review afterwards (edge case 21). So
    the status is 202 and the run id is real — and `admitted` is how the caller learns
    that nothing is going to happen. A bare 202 would read as success; a 403 would
    throw away the evidence.
    """
    await _apply(client, org)
    await client.post(f"/v1/control/organizations/{org}/departments/{DEPARTMENT}/stop", json={})

    response = await client.post(
        f"/v1/control/organizations/{org}/actors/{HEAD}/run", json={"mode": "weekly_plan"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["admitted"] is False
    assert body["refusal_reason"] == "KILL_SWITCH"
    assert body["status"] == "LIMIT_REACHED"
    assert body["run_id"], "the refusal is reviewable because the row exists"


async def test_an_unknown_actor_is_404(client: httpx.AsyncClient, org: OrganizationId) -> None:
    """The mapping in `api/errors.py`, exercised through the router that needs it most.

    `UnknownActorError` is a `SpecError`, and a catch-all checked first would answer
    400 — telling the caller their request was malformed when the request was fine and
    the actor is not there.
    """
    await _apply(client, org)
    response = await client.post(
        f"/v1/control/organizations/{org}/actors/nobody/run", json={"mode": "weekly_plan"}
    )
    assert response.status_code == 404


# --- the crank ------------------------------------------------------------------------


async def test_tick_fires_the_schedule_and_only_creates_runs(
    client: httpx.AsyncClient, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """The button is the conductor's work done now: cron fires become runs, and that is
    all it does. `start_run` writes and returns; a worker executes.

    What is asserted is that each fire produced a *run row with a frozen spec* — the
    output of admission. Deliberately not "and the run is still QUEUED": the moment a
    worker is running, that is a race, and a test that only passes on a machine with no
    worker is a test that will be deleted the first time it goes red for the right
    reason.
    """
    await _apply(client, org)
    body = (await client.post(f"/v1/control/organizations/{org}/tick")).json()
    assert "dispatched" in body
    started = [f for f in body["fired"] if not f["skipped"]]
    assert started, "testco's schedule has due occurrences"

    async with uow_factory() as uow:
        for fire in started:
            frozen = (
                await uow.session.execute(
                    text("SELECT count(*) FROM run_specs WHERE run_id = :id"),
                    {"id": fire["run_id"]},
                )
            ).scalar_one()
            assert frozen == 1, f"{fire['trigger']} was admitted, not executed, by the API"
