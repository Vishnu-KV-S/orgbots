"""Bot templates: export, share links, and making a bot from one.

What a template carries (the setup) and what it never does (what the bot learned or
was given); and the two rules an import keeps whoever wrote the template — allow rules
only when the person asks, routines paused. Postgres is `runtime_features_test`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.routines import RoutineSpec
from runtime.domain.templates import BotTemplate, TemplateRule, file_name, plan


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any
) -> AsyncIterator[httpx.AsyncClient]:
    from runtime.runtime.run_service import RunService

    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    app.state.service = RunService(uow_factory, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http


SOMEONE_ELSE = {ORG_HEADER: str(uuid.UUID("00000000-0000-4000-8000-00000000e15e"))}

BRIEF = {
    "mission": "Keep the team's reading list current.",
    "duties": ["Find new papers", "Summarise them"],
    "boundaries": ["Never buy anything"],
    "notes": "Prefer arXiv.",
}


async def _scout(api: httpx.AsyncClient) -> dict[str, Any]:
    """A bot with everything a template has, and everything it must not have."""
    bot = (
        await api.post(
            "/v1/bots",
            json={
                "name": "Scout",
                "label": "Research",
                "description": "Finds papers",
                "brief": BRIEF,
                "appearance": {"shape": "cube", "body": "#3366ff"},
            },
        )
    ).json()
    base = f"/v1/bots/{bot['id']}"
    for rule in (
        {"action_type": "click", "host": "Shop.Example", "decision": "allow"},
        {"action_type": "type", "host": "bank.example", "decision": "deny"},
        {"action_type": "use_connector", "host": "deepwiki", "decision": "ask"},
    ):
        assert (await api.post(f"{base}/rules", json=rule)).status_code == 201
    for routine in (
        {"name": "Digest", "instruction": "Send the week's papers", "cron": "0 8 * * 1",
         "timezone": "Asia/Kolkata"},
        {"name": "On push", "instruction": "Read the change", "kind": "event",
         "source": "webhook"},
    ):
        made = await api.post(f"{base}/routines", json=routine)
        assert made.status_code == 201, made.text
    await api.patch(base, json={"auto_review": True})
    await api.post(f"{base}/memories", json={"content": "The person's sister is Priya."})
    return bot


async def test_a_template_carries_the_setup_and_nothing_the_bot_learned(
    api: httpx.AsyncClient,
) -> None:
    bot = await _scout(api)
    hooks = (await api.get(f"/v1/bots/{bot['id']}/routines")).json()["routines"]
    token = next(r["hook_url"] for r in hooks if r["hook_url"]).rsplit("/", 1)[1]

    got = await api.get(f"/v1/bots/{bot['id']}/template")
    assert got.status_code == 200
    body = got.json()
    template = body["template"]
    assert body["file_name"] == "Scout.bot.json"
    assert template["format"] == "agent-org/bot-template" and template["version"] == 1
    assert (template["name"], template["label"]) == ("Scout", "Research")
    assert template["brief"]["mission"] == BRIEF["mission"]
    assert template["appearance"]["shape"] == "cube" and template["auto_review"] is True
    assert {(r["action_type"], r["decision"]) for r in template["rules"]} == {
        ("click", "allow"), ("type", "deny"), ("use_connector", "ask")
    }
    assert sorted(r["name"] for r in template["routines"]) == ["Digest", "On push"]
    assert "Priya" not in got.text, "memories stay with the bot"
    assert token not in got.text, "an event routine's address stays with the bot"
    assert bot["id"] not in got.text and bot["actor_name"] not in got.text


async def test_importing_a_file_keeps_asks_and_denies_and_pauses_every_routine(
    api: httpx.AsyncClient,
) -> None:
    source = await _scout(api)
    template = (await api.get(f"/v1/bots/{source['id']}/template")).json()["template"]

    shown = await api.post("/v1/templates/preview", json={"template": template},
                           headers=SOMEONE_ELSE)
    assert shown.status_code == 200, shown.text
    plan_ = shown.json()["plan"]
    assert [r["decision"] for r in plan_["allows"]] == ["allow"]
    assert {r["decision"] for r in plan_["rules"]} == {"ask", "deny"}
    assert {r["name"]: r["when"] for r in plan_["routines"]}["Digest"].startswith("Every Monday")

    made = await api.post("/v1/templates/import", json={"template": template, "name": "My Scout"},
                          headers=SOMEONE_ELSE)
    assert made.status_code == 201, made.text
    bot = made.json()
    assert bot["name"] == "My Scout" and bot["id"] != source["id"]
    assert bot["brief"]["mission"] == BRIEF["mission"] and bot["auto_review"] is True
    assert bot["appearance"]["shape"] == "cube"
    rules = (await api.get(f"/v1/bots/{bot['id']}/rules", headers=SOMEONE_ELSE)).json()["rules"]
    assert {(r["action_type"], r["host"], r["decision"]) for r in rules} == {
        ("type", "bank.example", "deny"), ("use_connector", "deepwiki", "ask")
    }, "the allow rule was left out"
    routines = (await api.get(f"/v1/bots/{bot['id']}/routines", headers=SOMEONE_ELSE)).json()
    assert all(r["active"] is False for r in routines["routines"])
    old = (await api.get(f"/v1/bots/{source['id']}/routines")).json()["routines"]
    new_hook = next(r["hook_url"] for r in routines["routines"] if r["hook_url"])
    assert new_hook not in {r["hook_url"] for r in old}, "an event routine gets its own address"
    memories = (await api.get(f"/v1/bots/{bot['id']}/memories", headers=SOMEONE_ELSE)).json()
    assert memories["memories"] == []

    kept = await api.post("/v1/templates/import",
                          json={"template": template, "keep_allows": True},
                          headers=SOMEONE_ELSE)
    rules = (await api.get(f"/v1/bots/{kept.json()['id']}/rules", headers=SOMEONE_ELSE)).json()
    assert ("click", "shop.example", "allow") in {
        (r["action_type"], r["host"], r["decision"]) for r in rules["rules"]
    }


async def test_a_link_is_a_snapshot_counts_its_uses_and_can_be_turned_off(
    api: httpx.AsyncClient,
) -> None:
    bot = await _scout(api)
    links = f"/v1/bots/{bot['id']}/template-links"
    link = (await api.post(links)).json()
    assert len(link["token"]) >= 20 and link["uses"] == 0
    await api.patch(f"/v1/bots/{bot['id']}",
                    json={"brief": {**BRIEF, "mission": "Something private now."}})

    shared = await api.get(f"/v1/templates/shared/{link['token']}", headers=SOMEONE_ELSE)
    assert shared.status_code == 200
    assert shared.json()["template"]["brief"]["mission"] == BRIEF["mission"], "as it was shared"

    made = await api.post("/v1/templates/import", json={"token": link["token"]},
                          headers=SOMEONE_ELSE)
    assert made.status_code == 201 and made.json()["name"] == "Scout"
    assert (await api.get(links)).json()["links"][0]["uses"] == 1

    assert (await api.delete(f"{links}/{link['id']}")).status_code == 200
    assert (await api.get(links)).json()["links"] == []
    gone = await api.get(f"/v1/templates/shared/{link['token']}", headers=SOMEONE_ELSE)
    assert gone.status_code == 410 and "turned off" in gone.json()["detail"]
    again = await api.post("/v1/templates/import", json={"token": link["token"]},
                           headers=SOMEONE_ELSE)
    assert again.status_code == 410
    unknown = await api.get("/v1/templates/shared/" + "x" * 24, headers=SOMEONE_ELSE)
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"version": 2, "skills": ["from the future"]}, "newer version"),
        ({"format": "a-recipe"}, "not a bot template"),
        ({"routines": [{"name": "Spam", "instruction": "x", "cron": "* * * * *"}]}, "Spam"),
        ({"routines": [{"name": "A", "instruction": "x", "cron": "0 8 * * *"},
                       {"name": "a", "instruction": "y", "cron": "0 9 * * *"}]}, "same name"),
        ({"memories": ["sneaked in"]}, "memories"),
        ({"rules": [{"action_type": "click", "decision": "always"}]}, "decision"),
    ],
)
async def test_a_template_that_cannot_be_imported_says_why_and_makes_nothing(
    api: httpx.AsyncClient, change: dict[str, Any], says: str
) -> None:
    template = {"format": "agent-org/bot-template", "version": 1, "name": "Odd", **change}
    before = len((await api.get("/v1/bots")).json()["bots"])
    for path, body in (("/v1/templates/preview", {"template": template}),
                       ("/v1/templates/import", {"template": template})):
        refused = await api.post(path, json=body)
        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert isinstance(detail, str) and says in detail, "a sentence the person can read"
    assert len((await api.get("/v1/bots")).json()["bots"]) == before


def test_the_plan_dedupes_rules_and_never_activates_a_routine() -> None:
    template = BotTemplate(
        name="A/B: test?",
        rules=[
            TemplateRule(action_type="click", host="X.com", decision="ask"),
            TemplateRule(action_type="click", host="x.com ", decision="deny"),
            TemplateRule(action_type="buy", host="x.com", decision="allow"),
        ],
        routines=[RoutineSpec(name="R", instruction="go", cron="0 8 * * *", active=True)],
    )
    without = plan(template, keep_allows=False)
    assert [(r.action_type, r.host, r.decision) for r in without.rules] == [
        ("click", "x.com", "ask")
    ]
    assert [r.decision for r in plan(template, keep_allows=True).rules] == ["ask", "allow"]
    assert [r.active for r in without.routines] == [False]
    assert file_name(template) == "A_B_ test_.bot.json"
