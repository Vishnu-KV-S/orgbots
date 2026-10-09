"""Enterprise: policies, team secrets, the audit trail, SCIM and telemetry export.

The API runs with members against `runtime_features_test` (the `team` fixture from the
members tests: Olive the owner, signed in). The computer's allowlist is checked in real
Chrome; the gateway tools against a recording computer; telemetry against a recording
collector.
"""

# ruff: noqa: F811 — the members tests' fixtures (`team`, `keys`) are imported by name,
# which is how pytest finds them, and each test that takes one looks like a redefinition.

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from runtime.domain.policies import Policy, PolicyError, check_secret, clean_hosts
from tests.test_bot_members import Team, keys, team  # noqa: F401

# --- the rules ------------------------------------------------------------------------------


def test_hosts_are_cleaned_and_cover_their_subdomains() -> None:
    assert clean_hosts(["https://Docs.Example.com/path", "*.github.com", " api.x.io:443 "]) == [
        "docs.example.com",
        "github.com",
        "api.x.io",
    ]
    for bad in ("not a host", "localhost", "a..b"):
        with pytest.raises(PolicyError):
            clean_hosts([bad])
    policy = Policy(network="allowlist", allowed_hosts=("github.com",))
    assert policy.url_allowed("https://api.github.com/repos")
    assert not policy.url_allowed("https://evilgithub.com/")
    assert not policy.url_allowed("file:///etc/passwd")
    assert policy.url_allowed("about:blank") and policy.url_allowed("data:text/plain,hi")
    assert Policy().url_allowed("https://anything.test/")


def test_a_team_secret_is_an_environment_variable_with_limits() -> None:
    check_secret("GITHUB_TOKEN", "ghp_x", others={})
    for name in ("lower", "1ABC", "PATH", "RUNTIME_KEY", "LD_PRELOAD"):
        with pytest.raises(PolicyError):
            check_secret(name, "v", others={})
    with pytest.raises(PolicyError, match="32 KB"):
        check_secret("BIG", "x" * (33 * 1024), others={})
    with pytest.raises(PolicyError, match="96 KB"):
        check_secret(
            "MORE", "x" * 1024, others={"A": 32 * 1024, "B": 32 * 1024, "C": 31 * 1024 + 1}
        )
    with pytest.raises(PolicyError, match="at most 100"):
        check_secret("NEW", "x", others={f"S{i}": 1 for i in range(100)})


# --- policies and secrets through the API ---------------------------------------------------


async def test_admins_set_the_rules_and_members_read_them(team: Team, keys: None) -> None:
    bob = await team.invite("bob@acme.test", "Bob")
    assert (await bob.get("/v1/admin/policy")).json()["network"] == "open"
    refused = await bob.put(
        "/v1/admin/policy", json={"network": "allowlist", "allowed_hosts": ["x.com"]}
    )
    assert refused.status_code == 403
    empty = await team.owner.put("/v1/admin/policy", json={"network": "allowlist"})
    assert empty.status_code == 422 and "at least one host" in empty.json()["detail"]
    saved = await team.owner.put(
        "/v1/admin/policy",
        json={
            "network": "allowlist",
            "allowed_hosts": ["https://github.com", "docs.python.org"],
            "require_review": True,
            "template_links": False,
        },
    )
    assert saved.json()["allowed_hosts"] == ["github.com", "docs.python.org"]

    put = await team.owner.put("/v1/admin/secrets/GITHUB_TOKEN", json={"value": "ghp_s3cret_val"})
    assert put.status_code == 200
    listed = await team.owner.get("/v1/admin/secrets")
    assert [s["name"] for s in listed.json()["secrets"]] == ["GITHUB_TOKEN"]
    assert "ghp_s3cret_val" not in listed.text
    assert (await bob.get("/v1/admin/secrets")).status_code == 403
    assert (await bob.put("/v1/admin/secrets/X_Y", json={"value": "v"})).status_code == 403
    bad = await team.owner.put("/v1/admin/secrets/PATH", json={"value": "v"})
    assert bad.status_code == 422

    trail = (await team.owner.get("/v1/admin/audit")).json()["events"]
    actions = [e["action"] for e in trail]
    assert {
        "member.owner_added",
        "member.invited",
        "member.joined",
        "policy.changed",
        "secret.set",
    } <= set(actions)
    secret_line = next(e for e in trail if e["action"] == "secret.set")
    assert secret_line["target"] == "GITHUB_TOKEN" and "ghp_" not in json.dumps(trail)
    changed = next(e for e in trail if e["action"] == "policy.changed")["detail"]
    assert changed["network"] == {"from": "open", "to": "allowlist"}
    assert (await bob.get("/v1/admin/audit")).status_code == 403


async def test_policies_hold_links_apps_and_review(team: Team, uow_factory: Any) -> None:
    from runtime.org.bots import BotService

    bob = await team.invite("bob@acme.test", "Bob")
    bot = (await team.owner.post("/v1/bots", json={"name": "Scout"})).json()
    link = (await team.owner.post(f"/v1/bots/{bot['id']}/template-links")).json()
    assert (await bob.get(f"/v1/templates/shared/{link['token']}")).status_code == 200

    await team.owner.put("/v1/admin/policy", json={"template_links": False, "require_review": True})
    gone = await bob.get(f"/v1/templates/shared/{link['token']}")
    assert gone.status_code == 410 and "turned its template links off" in gone.json()["detail"]
    blocked = await team.owner.post(f"/v1/bots/{bot['id']}/template-links")
    assert blocked.status_code == 403 and "save a file" in blocked.json()["detail"]

    app = {"title": "Wiki", "url": "https://mcp.example.com/mcp"}
    member_app = await bob.post("/v1/connectors", json=app)
    assert member_app.status_code == 403 and "admins connect apps" in member_app.json()["detail"]
    await team.owner.put(
        "/v1/admin/policy",
        json={"network": "allowlist", "allowed_hosts": ["github.com"], "members_add_apps": True},
    )
    off_list = await bob.post("/v1/connectors", json=app)
    assert off_list.status_code == 422 and "mcp.example.com" in off_list.json()["detail"]

    await team.owner.put("/v1/admin/policy", json={"require_review": True})
    async with uow_factory() as uow:
        row = await uow.bots.get(uuid.UUID(bot["id"]))
    assert row is not None and row.auto_review is False, "the bot's own switch is untouched"
    seen = await BotService(uow_factory).get(row.id)
    assert seen is not None and seen.auto_review is True, "the turn sees it on"


# --- SCIM --------------------------------------------------------------------------------------


async def test_scim_provisions_updates_and_deactivates_members(team: Team) -> None:
    made = await team.owner.post("/v1/admin/scim/token")
    assert made.status_code == 201
    token = made.json()["token"]
    assert made.json()["base_url"].endswith("/scim/v2")
    status = (await team.owner.get("/v1/admin/scim")).json()
    assert status["configured"] and status["hint"] == token[-4:] and token not in str(status)

    idp = team.anonymous()
    idp.headers["authorization"] = f"Bearer {token}"
    assert (await team.anonymous().get("/scim/v2/Users")).status_code == 401
    config = await idp.get("/scim/v2/ServiceProviderConfig")
    assert config.json()["patch"]["supported"] is True
    assert config.headers["content-type"].startswith("application/scim+json")

    none = await idp.get("/scim/v2/Users", params={"filter": 'userName eq "dana@acme.test"'})
    assert none.json()["totalResults"] == 0
    created = await idp.post(
        "/scim/v2/Users",
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "Dana@Acme.test",
            "externalId": "00u1",
            "name": {"givenName": "Dana", "familyName": "Lee"},
            "emails": [{"value": "dana@acme.test", "primary": True}],
            "active": True,
        },
    )
    assert created.status_code == 201, created.text
    user = created.json()
    assert (user["userName"], user["displayName"], user["active"]) == (
        "dana@acme.test",
        "Dana Lee",
        True,
    )
    again = await idp.post("/scim/v2/Users", json={"userName": "dana@acme.test"})
    assert again.status_code == 409 and again.json()["scimType"] == "uniqueness"
    found = await idp.get("/scim/v2/Users", params={"filter": 'externalId eq "00u1"'})
    assert [u["id"] for u in found.json()["Resources"]] == [user["id"]]

    link = (await team.owner.post(f"/v1/members/{user['id']}/sign-in-link")).json()["link"]
    dana = await team.sign_in(link)
    assert (await dana.get("/v1/bots")).status_code == 200

    entra = await idp.patch(
        f"/scim/v2/Users/{user['id']}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
        },
    )
    assert entra.status_code == 200 and entra.json()["active"] is False
    assert (await dana.get("/v1/bots")).status_code == 401, "deactivating signs them out"
    okta = await idp.patch(
        f"/scim/v2/Users/{user['id']}",
        json={
            "Operations": [{"op": "replace", "value": {"active": True, "displayName": "Dana L"}}],
        },
    )
    assert okta.json()["active"] is True and okta.json()["displayName"] == "Dana L"
    replaced = await idp.put(
        f"/scim/v2/Users/{user['id']}",
        json={"userName": "dana.lee@acme.test", "name": {"formatted": "Dana Lee"}, "active": True},
    )
    assert replaced.json()["userName"] == "dana.lee@acme.test"
    assert (await idp.delete(f"/scim/v2/Users/{user['id']}")).status_code == 204
    assert (await idp.get(f"/scim/v2/Users/{user['id']}")).json()["active"] is False

    members = (await team.owner.get("/v1/members")).json()["members"]
    olive = next(m for m in members if m["email"] == "olive@acme.test")
    last = await idp.patch(
        f"/scim/v2/Users/{olive['id']}",
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert last.status_code == 409 and "last owner" in last.json()["detail"]

    trail = [
        e["action"]
        for e in (await team.owner.get("/v1/admin/audit", params={"action": "scim."})).json()[
            "events"
        ]
    ]
    assert {
        "scim.user_created",
        "scim.user_deactivated",
        "scim.user_reactivated",
        "scim.token_made",
    } <= set(trail)

    await team.owner.delete("/v1/admin/scim")
    assert (await idp.get("/scim/v2/Users")).status_code == 401, "a revoked token stops"


# --- the gateway tools --------------------------------------------------------------------------


@pytest.fixture
def org_ctx() -> Any:
    from runtime.gateway.tools import ToolContext

    return ToolContext(
        run_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        logical_call_id="c",
        idempotency_key="k",
        marker="m",
        attempt=1,
    )


async def _policy(uow_factory: Any, org: str, policy: Policy) -> None:
    from runtime.gateway.builtin import profiles
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(uuid.UUID(org), "x")
    async with uow_factory.transaction() as uow:
        await uow.policies.save(uuid.UUID(org), policy)
    profiles._policies.clear()


async def test_the_browser_tool_refuses_a_host_off_the_list_and_tells_the_computer(
    uow_factory: Any, settings: Any, org_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.gateway.builtin import browser

    await _policy(
        uow_factory,
        org_ctx.organization_id,
        Policy(network="allowlist", allowed_hosts=("github.com",)),
    )
    seen: list[httpx.Request] = []

    def computer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "snapshot": {}, "url": "", "rendered": ""})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        browser.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(computer), **kw),
    )
    (_, observe), (_, act) = browser.build(settings, uow_factory)
    screen = str(uuid.uuid4())
    refused = await act(
        org_ctx,
        browser.ActArgs(
            screen_id=screen, action={"type": "navigate", "url": "https://evil.test/x"}
        ),
    )
    assert refused.ok is False and "allowlist does not include evil.test" in (refused.error or "")
    assert seen == [], "never sent"
    async with uow_factory() as uow:
        (blocked,) = await uow.org_audit.recent(uuid.UUID(org_ctx.organization_id))
    assert (blocked.action, blocked.target) == ("network.blocked", "evil.test")
    ok = await act(
        org_ctx,
        browser.ActArgs(
            screen_id=screen, action={"type": "navigate", "url": "https://api.github.com/"}
        ),
    )
    assert ok.ok is True
    assert seen[0].headers["x-allow-hosts"] == "github.com"
    await observe(org_ctx, browser.ObserveArgs(screen_id=screen))
    assert seen[1].headers["x-allow-hosts"] == "github.com"


async def test_sandboxed_commands_get_team_secrets_and_no_network_under_an_allowlist(
    uow_factory: Any, settings: Any, org_ctx: Any, keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.domain.scrub import scrub_text
    from runtime.gateway.builtin import terminal
    from runtime.runtime.enterprise import EnterpriseService

    await _policy(
        uow_factory,
        org_ctx.organization_id,
        Policy(network="allowlist", allowed_hosts=("github.com",)),
    )
    await EnterpriseService(uow_factory, settings).put_secret(
        None, uuid.UUID(org_ctx.organization_id), "API_TOKEN", "tok-0123456789abcdef"
    )
    sent: list[dict[str, Any]] = []

    def computer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ok": True, "exit_code": 0, "stdout": "", "mode": "sandbox"}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        terminal.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(computer), **kw),
    )
    tools = {d.name: fn for d, fn in terminal.build(settings, uow_factory)}
    await tools["terminal.run"](org_ctx, terminal.RunArgs(screen_id="s", command="env"))
    assert sent[0]["secrets"] == {"API_TOKEN": "tok-0123456789abcdef"}
    assert sent[0]["network"] is False
    cleaned, hits = scrub_text("printed tok-0123456789abcdef by mistake")
    assert "tok-0123456789abcdef" not in cleaned and hits, "a printed secret is redacted"


async def test_the_browser_itself_blocks_hosts_off_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.server
    import threading

    from runtime.computer.app import create_app as computer_app
    from runtime.computer.browser import url_allowed

    assert url_allowed("https://a.github.com/x", ("github.com",))
    assert not url_allowed("https://github.com.evil.test/", ("github.com",))
    assert not url_allowed("https://user@evil.test/", ("github.com",))
    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<p>hello</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    app = computer_app(tmp_path / "profile", workspace=tmp_path / "ws")
    await app.state.computer.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as http_:
            only_localhost = {"x-allow-hosts": "localhost"}

            async def go(url: str, headers: dict[str, str]) -> dict[str, Any]:
                got = await http_.post(
                    "/screens/a/act",
                    headers=headers,
                    json={"action": {"type": "navigate", "url": url}},
                )
                return dict(got.json())

            allowed = await go(f"http://localhost:{port}/", only_localhost)
            blocked = await go(f"http://127.0.0.1:{port}/", only_localhost)
            reopened = await go(f"http://127.0.0.1:{port}/", {"x-allow-hosts": "*"})
            # The policy narrows while a screen shows a host it no longer allows.
            await http_.post(
                "/screens/a/observe", headers={"x-allow-hosts": "example.com"}, json={}
            )
            after = (await http_.post("/screens/a/observe", json={})).json()
    finally:
        await app.state.computer.stop()
        server.shutdown()
    assert allowed["ok"] is True and "hello" in allowed["rendered"]
    assert blocked["ok"] is False and "ERR_BLOCKED_BY_CLIENT" in (blocked["error"] or "")
    assert reopened["ok"] is True, "an open policy lifts the block"
    assert after["url"] == "about:blank" and "hello" not in after["rendered"], (
        "a page the new policy forbids is not left on the screen to be read"
    )


# --- telemetry export ----------------------------------------------------------------------------


async def test_events_and_tool_calls_reach_the_collector_once_and_wait_out_a_failure(
    team: Team, uow_factory: Any, keys: None
) -> None:
    from runtime.gateway.otel import OTLPSender
    from runtime.runtime.telemetry import TelemetryExporter

    received: list[tuple[dict[str, str], dict[str, Any]]] = []
    answer = {"status": 200}

    def collector(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/logs"
        received.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(answer["status"], json={})

    saved = await team.owner.put(
        "/v1/admin/telemetry",
        json={
            "endpoint": "https://collector.acme.test",
            "headers": {"Authorization": "Bearer otlp"},
        },
    )
    assert saved.status_code == 200 and saved.json()["has_headers"] and "otlp" not in saved.text
    async with uow_factory.transaction() as uow:
        await uow.otel.cursor(team.org)  # starts from now
        await uow.session.execute(
            text(
                "INSERT INTO audit_logs (organization_id, gateway, subject, decision, check_name, "
                "actor_name) VALUES (:org, 'tool', 'browser.act@1', 'denied', 'rule', 'bot-scout')"
            ),
            {"org": team.org},
        )
    await team.invite("bob@acme.test", "Bob")

    exporter = TelemetryExporter(
        uow_factory, team.settings, OTLPSender(transport=httpx.MockTransport(collector))
    )
    answer["status"] = 503
    failed = await exporter.tick()
    assert failed.failed == 1
    status = (await team.owner.get("/v1/admin/telemetry")).json()
    assert "503" in status["last_error"]

    answer["status"] = 200
    received.clear()
    done = await exporter.tick()
    assert done.events >= 3 and done.actions == 1
    (h1, events), (_, actions) = received
    assert h1["authorization"] == "Bearer otlp"
    records = events["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    names = [r["body"]["stringValue"] for r in records]
    assert {"member.invited", "member.joined", "telemetry.changed"} <= set(names)
    flat = json.dumps(events)
    assert "bob@acme.test" not in flat and "olive@acme.test" not in flat, "no emails by default"
    (call,) = actions["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {a["key"]: next(iter(a["value"].values())) for a in call["attributes"]}
    assert (attrs["tool.name"], attrs["tool.decision"], call["severityText"]) == (
        "browser.act@1",
        "denied",
        "WARN",
    )
    received.clear()
    assert (await exporter.tick()).events == 0 and received == [], "each once"
