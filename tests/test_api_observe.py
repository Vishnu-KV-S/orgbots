"""The read-only observation surface.

What is asserted here is what a viewer would be *wrong* about if it broke: that
an organization with no live run does not read as running, that an engaged kill
switch outranks a live run, that the hierarchy comes back as edges a canvas can
lay out, and that nothing on this surface can change anything.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.api.app import create_app
from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_runtime import ECHO_SPEC, HASHER_SPEC

pytestmark = pytest.mark.integration

DEPARTMENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
GOAL_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
PROJECT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest_asyncio.fixture
async def org(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> AsyncIterator[OrganizationId]:
    """A two-actor department with a goal, a project and one open task.

    Built with SQL rather than through the spec compiler on purpose: this module
    is testing the *reader*, and going through the writer would make a compiler
    change look like a reader regression.
    """
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, "observed-co")
    head = await registrar.publish_actor(organization_id, ECHO_SPEC)
    report = await registrar.publish_actor(organization_id, HASHER_SPEC)

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                """
                INSERT INTO departments (id, organization_id, name, head_actor_name,
                                         description, memory_scope_id)
                VALUES (:id, :org, 'growth', :head, 'ships things', :mem)
                """
            ),
            {
                "id": DEPARTMENT_ID,
                "org": organization_id,
                "head": ECHO_SPEC.name,
                "mem": DEPARTMENT_ID,
            },
        )
        await uow.session.execute(
            text(
                """
                UPDATE actors SET department = 'growth', department_id = :dept,
                                  role_name = :role, reports_to = :boss
                 WHERE id = :id
                """
            ),
            {"dept": DEPARTMENT_ID, "role": "ic", "boss": ECHO_SPEC.name, "id": report.actor_id},
        )
        await uow.session.execute(
            text(
                """
                UPDATE actors SET department = 'growth', department_id = :dept,
                                  role_name = :role
                 WHERE id = :id
                """
            ),
            {"dept": DEPARTMENT_ID, "role": "head", "id": head.actor_id},
        )
        await uow.session.execute(
            text(
                """
                INSERT INTO goals (id, organization_id, name, statement, horizon)
                VALUES (:id, :org, 'be-known', 'Be known by the people who would use it',
                        'quarter')
                """
            ),
            {"id": GOAL_ID, "org": organization_id},
        )
        await uow.session.execute(
            text(
                """
                INSERT INTO projects (id, organization_id, goal_id, name, description,
                                      owner_actor_id)
                VALUES (:id, :org, :goal, 'weekly-loop', 'plan, ship, measure', :owner)
                """
            ),
            {"id": PROJECT_ID, "org": organization_id, "goal": GOAL_ID, "owner": head.actor_id},
        )
        await uow.session.execute(
            text(
                """
                INSERT INTO tasks (id, organization_id, correlation_id, title, objective,
                                   acceptance_criteria, assignee_name, output_schema_ref,
                                   input, status)
                VALUES (:id, :org, :corr, 'Draft the thing', 'One post',
                        '{}'::jsonb, :assignee, 'Draft@1', '{}'::jsonb, 'ASSIGNED')
                """
            ),
            {
                "id": uuid.uuid4(),
                "org": organization_id,
                "corr": uuid.uuid4(),
                "assignee": HASHER_SPEC.name,
            },
        )
    yield organization_id


@pytest_asyncio.fixture
async def client(
    settings: Settings, uow_factory: UnitOfWorkFactory
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_listing_needs_no_organization_header(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """The header the control surface requires would be a chicken-and-egg problem
    here: a viewer asks *which* organizations exist before it can name one."""
    response = await client.get("/v1/observe/organizations")
    assert response.status_code == 200
    orgs = response.json()["organizations"]
    assert [o["name"] for o in orgs] == ["observed-co"]
    assert orgs[0]["actors"] == 2
    assert orgs[0]["departments"] == 1
    assert orgs[0]["tasks_open"] == 1


async def test_organization_with_no_live_run_is_idle(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    body = (await client.get("/v1/observe/organizations")).json()["organizations"][0]
    assert body["status"] == "idle"
    assert body["runs_active"] == 0


async def test_engaged_kill_switch_outranks_a_live_run(
    client: httpx.AsyncClient, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """The one status the viewer must not get wrong. A halted org whose runs are
    draining looks busy in the run counts and is not."""
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                """
                INSERT INTO kill_switches (id, organization_id, scope_type, scope_id, mode,
                                           reason, engaged_by)
                VALUES (:id, :org, 'org', NULL, 'halt', 'spend', 'operator')
                """
            ),
            {"id": uuid.uuid4(), "org": org},
        )
    body = (await client.get("/v1/observe/organizations")).json()["organizations"][0]
    assert body["status"] == "halted"


async def test_graph_returns_the_hierarchy_as_edges(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    response = await client.get(f"/v1/observe/organizations/{org}/graph")
    assert response.status_code == 200
    body = response.json()

    kinds = {node["type"] for node in body["nodes"]}
    assert kinds == {"organization", "department", "actor", "goal", "project"}

    by_label = {node["label"]: node for node in body["nodes"]}
    assert by_label[HASHER_SPEC.name]["data"]["reports_to"] == ECHO_SPEC.name
    assert by_label[HASHER_SPEC.name]["data"]["tasks"]["open"] == 1
    assert by_label["growth"]["data"]["head"] == ECHO_SPEC.name

    edges = {(edge["kind"], edge["source"], edge["target"]) for edge in body["edges"]}
    org_node = f"org:{org}"
    dept_node = f"dept:{DEPARTMENT_ID}"
    head_node = by_label[ECHO_SPEC.name]["id"]
    report_node = by_label[HASHER_SPEC.name]["id"]

    assert ("contains", org_node, dept_node) in edges
    assert ("heads", dept_node, head_node) in edges
    assert ("reports_to", head_node, report_node) in edges
    assert ("project", f"goal:{GOAL_ID}", f"project:{PROJECT_ID}") in edges
    assert ("owns", f"project:{PROJECT_ID}", head_node) in edges


async def test_an_actor_appears_exactly_once_under_its_manager(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    """A second parent edge would put the same actor in two places on the canvas."""
    body = (await client.get(f"/v1/observe/organizations/{org}/graph")).json()
    by_label = {node["label"]: node for node in body["nodes"]}
    report_node = by_label[HASHER_SPEC.name]["id"]
    parents = [
        edge
        for edge in body["edges"]
        if edge["target"] == report_node and edge["kind"] in ("reports_to", "member", "heads")
    ]
    assert len(parents) == 1


async def test_unknown_organization_is_404_not_an_empty_graph(
    client: httpx.AsyncClient,
) -> None:
    """An empty canvas and a wrong id look identical to a viewer. They must not."""
    response = await client.get(f"/v1/observe/organizations/{uuid.uuid4()}/graph")
    assert response.status_code == 404


async def test_activity_returns_tasks_and_events(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    response = await client.get(f"/v1/observe/organizations/{org}/activity?limit=5")
    assert response.status_code == 200
    body = response.json()
    assert [task["title"] for task in body["tasks"]] == ["Draft the thing"]
    assert body["runs"] == []
    assert body["events"] == []


async def test_actor_detail_carries_the_published_spec(
    client: httpx.AsyncClient, org: OrganizationId
) -> None:
    graph = (await client.get(f"/v1/observe/organizations/{org}/graph")).json()
    actor_id = next(
        node["data"]["id"] for node in graph["nodes"] if node["label"] == ECHO_SPEC.name
    )

    response = await client.get(f"/v1/observe/actors/{actor_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == ECHO_SPEC.name
    assert body["spec"]["graph_ref"] == ECHO_SPEC.graph_ref
    assert body["versions"][0]["version"] == 1
    assert [report["name"] for report in body["reports"]] == [HASHER_SPEC.name]

    assert (await client.get(f"/v1/observe/actors/{uuid.uuid4()}")).status_code == 404


async def test_the_surface_is_read_only(client: httpx.AsyncClient, org: OrganizationId) -> None:
    """Nothing here accepts a write. A viewer that could start a run would be
    doing it around the admission, authority and budget checks."""
    for method, path in (
        ("post", f"/v1/observe/organizations/{org}/graph"),
        ("delete", f"/v1/observe/organizations/{org}/graph"),
        ("post", "/v1/observe/organizations"),
    ):
        response = await getattr(client, method)(path)
        assert response.status_code == 405, path
