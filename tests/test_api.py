"""API surface.

The assertions that matter here are about what the API refuses and what it
promises: 202 rather than 201, a repeat that is not a new run, a health check that
tells the truth, and a stream that can be resumed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import ORG_HEADER, create_app
from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService
from runtime.settings import Settings
from tests.conftest_runtime import ECHO_SPEC, HASHER_SPEC, Runtime, build_runtime

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


@pytest_asyncio.fixture
async def client(
    rt: Runtime, settings: Settings, organization_id: OrganizationId
) -> AsyncIterator[httpx.AsyncClient]:
    """Wire the app to the same components the fixture runtime uses, so a run
    started over HTTP is executed by the same worker the test can pump."""
    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = rt.uow
    app.state.streams = rt.streams
    app.state.service = rt.service

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http


async def test_healthz_reports_reachability(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["redis"] is True


async def test_post_runs_returns_202_and_does_not_execute(
    client: httpx.AsyncClient, rt: Runtime
) -> None:
    response = await client.post(
        "/v1/runs",
        json={"actor": "echo-agent", "input": {"message": "hi"}, "idempotency_key": "api-1"},
    )
    assert response.status_code == 202, "202 Accepted — the run is admitted, not finished"
    body = response.json()
    assert body["created"] is True
    assert body["status"] == "QUEUED"
    assert len(body["spec_hash"]) == 32

    detail = await client.get(f"/v1/runs/{body['run_id']}")
    assert detail.json()["status"] == "QUEUED", "the API must not have run the graph"


async def test_a_repeated_idempotency_key_returns_the_same_run(
    client: httpx.AsyncClient,
) -> None:
    first = await client.post(
        "/v1/runs", json={"actor": "echo-agent", "idempotency_key": "api-same"}
    )
    second = await client.post(
        "/v1/runs",
        json={"actor": "echo-agent", "input": {"other": 1}, "idempotency_key": "api-same"},
    )
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False, (
        "a caller retrying must be able to tell it was a retry, or double-submission "
        "bugs on their side stay invisible"
    )


async def test_get_run_shows_the_full_picture_after_execution(
    client: httpx.AsyncClient, rt: Runtime
) -> None:
    started = await client.post(
        "/v1/runs",
        json={"actor": "echo-agent", "input": {"sideeffect": {"n": 1}}, "idempotency_key": "api-2"},
    )
    run_id = started.json()["run_id"]
    await rt.pump()

    body = (await client.get(f"/v1/runs/{run_id}")).json()
    assert body["status"] == "SUCCESS"
    assert body["fence"] == 1
    assert body["actor_version"] == 1
    assert len(body["effects"]) == 1
    assert body["effects"][0]["status"] == "COMMITTED"
    assert body["effects"][0]["tool"] == "fixture.sideeffect@1"
    assert body["output"]["effect"]["replayed"] is False
    assert body["cost_cents"] == 0


async def test_an_unknown_actor_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/runs", json={"actor": "not-a-thing", "idempotency_key": "api-404"}
    )
    assert response.status_code == 404


async def test_a_missing_org_header_is_400(rt: Runtime, settings: Settings) -> None:
    app = create_app(settings)
    app.state.uow = rt.uow
    app.state.streams = rt.streams
    app.state.service = rt.service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/v1/runs", json={"actor": "echo-agent", "idempotency_key": "k"})
    assert response.status_code == 400
    assert ORG_HEADER in response.json()["detail"]


async def test_a_run_from_another_org_is_not_visible(
    client: httpx.AsyncClient, rt: Runtime
) -> None:
    """Scoping every read by org from day one. Retrofitting it is the expensive
    version of this decision."""
    started = await client.post(
        "/v1/runs", json={"actor": "echo-agent", "idempotency_key": "api-scope"}
    )
    run_id = started.json()["run_id"]
    response = await client.get(f"/v1/runs/{run_id}", headers={ORG_HEADER: str(uuid.uuid4())})
    assert response.status_code == 404


async def test_unknown_body_fields_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/runs",
        json={"actor": "echo-agent", "idempotency_key": "k", "durabilty": "sync"},
    )
    assert response.status_code == 422


async def test_the_stream_replays_history_from_a_cursor(
    client: httpx.AsyncClient, rt: Runtime
) -> None:
    """The tail is backed by `events` in Postgres, so a reconnect with `?after=`
    gets everything it missed."""
    started = await client.post(
        "/v1/runs",
        json={"actor": "echo-agent", "input": {"message": "streamed"}, "idempotency_key": "api-3"},
    )
    run_id = started.json()["run_id"]
    await rt.pump()

    async with client.stream("GET", f"/v1/runs/{run_id}/stream") as response:
        assert response.status_code == 200
        chunks = [chunk async for chunk in response.aiter_text()]
    body = "".join(chunks)
    assert "event: run.queued" in body
    assert "event: run.succeeded" in body
    assert "event: done" in body


async def test_the_stream_can_resume_past_events_already_seen(
    client: httpx.AsyncClient, rt: Runtime
) -> None:
    started = await client.post(
        "/v1/runs", json={"actor": "echo-agent", "idempotency_key": "api-4"}
    )
    run_id = started.json()["run_id"]
    await rt.pump()

    async with rt.uow() as uow:
        events = await uow.outbox.events_for_run(uuid.UUID(run_id))
    first_id = events[0]["id"]

    async with client.stream(
        "GET", f"/v1/runs/{run_id}/stream", params={"after": first_id}
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: run.queued" not in body, "already-seen events must not be resent"
    assert "event: run.succeeded" in body


async def test_a_hybrid_actor_is_501_not_500(
    client: httpx.AsyncClient, rt: Runtime, organization_id: OrganizationId
) -> None:
    """`NotImplementedError` is a statement about the runtime, not a crash. It
    deserves the status code that says so."""
    from sqlalchemy import text

    async with rt.uow.transaction() as uow:
        await uow.session.execute(
            text(
                """
                UPDATE actor_versions SET spec = jsonb_set(spec, '{kind}', '"hybrid"')
                 WHERE actor_id = (SELECT id FROM actors WHERE name = 'echo-agent')
                """
            )
        )
    response = await client.post(
        "/v1/runs", json={"actor": "echo-agent", "idempotency_key": "api-hybrid"}
    )
    assert response.status_code == 501
    assert "HYBRID" in response.json()["detail"]


async def test_registering_an_actor_is_versioned(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, settings: Settings
) -> None:
    """A republish is a new version, always — otherwise "which version ran this"
    stops being answerable at the moment someone asks."""
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, "acme")
    first = await registrar.publish_actor(organization_id, ECHO_SPEC)
    second = await registrar.publish_actor(organization_id, ECHO_SPEC)
    other = await registrar.publish_actor(organization_id, HASHER_SPEC)

    assert (first.version, second.version) == (1, 2)
    assert first.spec_hash == second.spec_hash, "an identical spec hashes identically"
    assert first.actor_id == second.actor_id
    assert other.actor_id != first.actor_id

    result = await RunService(uow_factory, settings=settings).start_run(
        __import__("runtime.domain.specs", fromlist=["StartRunRequest"]).StartRunRequest(
            organization_id=organization_id,
            actor_name="echo-agent",
            idempotency_key="version-check",
        )
    )
    async with uow_factory() as uow:
        spec = await uow.runs.get_spec(result.run_id)
    assert spec is not None
    assert spec.spec["spec"]["actor_version"] == 2, "the active version is the one that runs"
