"""Connectors: apps (MCP servers) a person connects and every bot can call.

A small MCP server runs in-process (`fake_mcp`) — it wants a bearer token, hands out a
session id, pages its tool list and answers one page as an event stream — so the
client, the API, the gateway tool and the graph are all exercised without the network.
Postgres is `runtime_features_test`, never the dev database.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, Response

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.bots import BotRule, BotStep, needs_approval
from runtime.domain.connectors import connector_name, read_only, render_connectors, signature
from runtime.gateway.mcp import Auth, MCPClient, MCPError, result_text
from runtime.graphs.registry import GRAPH_KEY, get_graph
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

TOKEN = "good-token"

SEARCH = {
    "name": "search_issues",
    "description": "Find issues by text.",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
    "annotations": {"readOnlyHint": True},
}
CREATE = {
    "name": "create_issue",
    "description": "Open a new issue.",
    "inputSchema": {"type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title"]},
}


def fake_mcp(*, token: str | None = TOKEN) -> FastAPI:
    app = FastAPI()
    app.state.calls = []

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        if token and request.headers.get("authorization") != f"Bearer {token}":
            return Response(status_code=401)
        body = await request.json()
        method = body.get("method")
        app.state.calls.append((method, request.headers.get("mcp-session-id")))
        if "id" not in body:
            return Response(status_code=202)
        rid = body["id"]

        def reply(result: dict[str, Any], *, sse: bool = False) -> Response:
            message = {"jsonrpc": "2.0", "id": rid, "result": result}
            if sse:
                data = f"event: message\ndata: {json.dumps(message)}\n\n"
                return Response(content=data, media_type="text/event-stream")
            return Response(content=json.dumps(message), media_type="application/json",
                            headers={"Mcp-Session-Id": "s-1"})

        if method == "initialize":
            return reply({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "Tracker", "version": "1"}})
        if request.headers.get("mcp-session-id") != "s-1":
            return Response(status_code=400, content="no session")
        if method == "tools/list":
            cursor = (body.get("params") or {}).get("cursor")
            if cursor is None:
                return reply({"tools": [SEARCH], "nextCursor": "p2"}, sse=True)
            return reply({"tools": [CREATE]})
        if method == "tools/call":
            params = body["params"]
            if params["name"] == "search_issues":
                return reply({"content": [{"type": "text",
                                           "text": f"#7 Login broken ({params['arguments']['query']})"}]})
            if params["name"] == "create_issue":
                return reply({"content": [{"type": "text", "text": "Created #8"}]})
            return Response(content=json.dumps({"jsonrpc": "2.0", "id": rid,
                                                "error": {"code": -32602, "message": "unknown tool"}}),
                            media_type="application/json")
        return Response(status_code=404)

    return app


def opener(server: FastAPI) -> Any:
    return lambda url, auth: MCPClient(
        "http://mcp.test/mcp", auth, transport=httpx.ASGITransport(app=server)
    )


# --- the client ----------------------------------------------------------------------------


async def test_the_client_initializes_keeps_its_session_pages_and_calls() -> None:
    server = fake_mcp()
    async with opener(server)("x", Auth("bearer", token=TOKEN)) as session:
        assert session.server["name"] == "Tracker"
        tools = await session.list_tools()
        result = await session.call_tool("search_issues", {"query": "login"})
        with pytest.raises(MCPError, match="unknown tool"):
            await session.call_tool("nope", {})
    assert [t["name"] for t in tools] == ["search_issues", "create_issue"]
    assert result_text(result) == ("#7 Login broken (login)", False)
    methods = [m for m, _ in server.state.calls]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    assert all(s == "s-1" for m, s in server.state.calls[1:]), "the session id rides along"


async def test_a_refused_token_says_so() -> None:
    with pytest.raises(MCPError, match="refused the credentials"):
        async with opener(fake_mcp())("x", Auth("bearer", token="wrong")):
            pass


def test_a_tool_reads_as_a_signature_and_a_list_reads_compactly() -> None:
    assert signature(CREATE) == "create_issue(title*, body)"
    assert read_only(SEARCH) and not read_only(CREATE)
    text = render_connectors([("tracker", "Issue tracker", [SEARCH, CREATE])])
    assert "search_issues(query*) [read] — Find issues by text." in text
    assert "create_issue(title*, body) — Open a new issue." in text
    assert connector_name("GitHub (work)") == "github-work"


def test_reading_runs_writing_asks_and_rules_are_by_app() -> None:
    write = BotStep(thought="t", action="use_connector", connector="tracker", tool="create_issue")
    read = BotStep(thought="t", action="use_connector", connector="tracker", tool="search_issues")

    def gate(step: BotStep, ro: bool, *rules: BotRule) -> tuple[bool, bool]:
        d = needs_approval(step, page_url="", element=None, rules=rules, read_only=ro)
        return d.ask, d.deny

    assert gate(read, True) == (False, False)
    assert gate(write, False) == (True, False)
    assert gate(write, False, BotRule("use_connector", "tracker", "allow")) == (False, False)
    assert gate(write, False, BotRule("use_connector", "other", "allow")) == (True, False)
    assert gate(read, True, BotRule("use_connector", "tracker", "deny")) == (False, True)


# --- the API and the gateway tool -----------------------------------------------------------


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    from runtime.runtime.run_service import RunService

    monkeypatch.setenv(
        "RUNTIME_CREDENTIAL_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode()
    )
    server = fake_mcp()
    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    app.state.service = RunService(uow_factory, settings=settings)
    app.state.mcp_connect = opener(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http, server


async def test_a_connector_is_added_only_when_it_connects_and_keeps_its_token_sealed(
    api: tuple[httpx.AsyncClient, FastAPI], uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    http, server = api
    wrong = await http.post("/v1/connectors", json={
        "title": "Tracker", "url": "https://mcp.test/mcp", "auth_kind": "bearer", "token": "bad"})
    assert wrong.status_code == 422 and "refused the credentials" in wrong.json()["detail"]
    assert (await http.get("/v1/connectors")).json()["connectors"] == []

    made = await http.post("/v1/connectors", json={
        "title": "Tracker", "url": "https://mcp.test/mcp", "auth_kind": "bearer", "token": TOKEN})
    assert made.status_code == 201, made.text
    view = made.json()
    assert view["name"] == "tracker" and view["has_token"] is True and TOKEN not in made.text
    assert [(t["name"], t["read_only"]) for t in view["tools"]] == [
        ("search_issues", True), ("create_issue", False)]
    async with uow_factory() as uow:
        row = await uow.connectors.by_name(organization_id, "tracker")
    assert row is not None and TOKEN.encode() not in (row.secret_ciphertext or b"")

    # The gateway tool opens the sealed token for the call, and only there.
    from runtime.gateway.builtin.connectors import CallArgs, build
    from runtime.gateway.tools import ToolContext

    _, call = build(settings, uow_factory, open_session=opener(server))
    ctx = ToolContext(
        run_id=str(uuid.uuid4()),
        organization_id=str(organization_id),
        logical_call_id="call-1",
        idempotency_key="key-1",
        marker="m",
        attempt=1,
    )
    done = await call(ctx, CallArgs(connector="tracker", tool="search_issues",
                                    arguments={"query": "login"}))
    assert done.ok and done.text == "#7 Login broken (login)"
    missing = await call(ctx, CallArgs(connector="nope", tool="x"))
    assert not missing.ok and "no connector" in (missing.error or "")

    off = await http.patch(f"/v1/connectors/{view['id']}", json={"enabled": False})
    assert off.json()["enabled"] is False
    refreshed = await http.post(f"/v1/connectors/{view['id']}/refresh")
    assert refreshed.json()["status"] == "ok"
    assert (await http.delete(f"/v1/connectors/{view['id']}")).status_code == 200


async def test_the_marketplace_lists_apps_and_installs_one(
    api: tuple[httpx.AsyncClient, FastAPI],
) -> None:
    http, _ = api
    market = (await http.get("/v1/marketplace/connectors")).json()["connectors"]
    github = next(c for c in market if c["key"] == "github")
    assert github["needs_token"] is True and github["installed"] is False
    assert next(c for c in market if c["key"] == "deepwiki")["checked"] is True
    refused = await http.post("/v1/marketplace/connectors/github", json={})
    assert refused.status_code == 422 and "needs a token" in refused.json()["detail"]
    installed = await http.post("/v1/marketplace/connectors/github", json={"token": TOKEN})
    assert installed.status_code == 201, installed.text
    assert installed.json()["catalog_key"] == "github"
    again = (await http.get("/v1/marketplace/connectors")).json()["connectors"]
    assert next(c for c in again if c["key"] == "github")["installed"] is True


# --- the graph -----------------------------------------------------------------------------


@dataclass
class _App:
    name: str
    title: str
    tools: list[dict[str, Any]]


@dataclass
class FakeApps:
    apps: list[_App] = field(default_factory=lambda: [_App("tracker", "Issue tracker",
                                                            [SEARCH, CREATE])])

    async def usable(self, organization_id: Any) -> list[_App]:
        return self.apps

    async def for_prompt(self, organization_id: Any) -> list[tuple[str, str, list[Any]]]:
        return [(a.name, a.title, a.tools) for a in self.apps]


class AppGateway(FakePageGateway):
    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        if call.tool == "connector.call@1":
            # Not under "tool": the call's own arguments have one (the app's tool).
            self.calls.append({"gateway": call.tool, **call.args})
            from tests.test_bots import _Result

            return _Result({"ok": True, "text": f"{call.args['tool']} ok", "is_error": False})
        return await super().execute(ctx, call)


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


DONE = {"thought": "Done", "action": "reply", "text": "Done."}


async def test_a_bot_reads_through_an_app_and_asks_before_writing() -> None:
    bots = FakeBots(_Bot(id=uuid.uuid4()))
    gateway = AppGateway()
    search = {"thought": "Look it up", "action": "use_connector", "connector": "tracker",
              "tool": "search_issues", "args": {"query": "login"}}
    create = {"thought": "File it", "action": "use_connector", "connector": "Tracker",
              "tool": "create_issue", "args": {"title": "Login broken"}}
    model = ScriptedModel([search, create])
    out = await _invoke(_Node(_Ctx(), gateway, model, _Org(bots, connectors=FakeApps())))

    assert "search_issues(query*) [read]" in model.system[0]
    assert "<<<RESULT\nsearch_issues ok\nRESULT>>>" in model.prompts[1]
    assert out["status"] == "awaiting_approval"
    calls = [c for c in gateway.calls if c.get("gateway") == "connector.call@1"]
    assert [c["tool"] for c in calls] == ["search_issues"], "the write waits for the person"
    (pid, pending), = bots.pendings.items()
    assert pending.action["rule"] == "use_connector" and pending.action["host"] == "tracker"

    pending.status = "allowed"
    await _invoke(_Node(_Ctx(), gateway, ScriptedModel([DONE]), _Org(bots, connectors=FakeApps())),
                  resume_pending_id=str(pid))
    created = [c for c in gateway.calls if c.get("gateway") == "connector.call@1"][-1]
    assert created["tool"] == "create_issue" and created["arguments"] == {"title": "Login broken"}


async def test_an_unknown_app_or_tool_names_what_exists() -> None:
    bots = FakeBots(_Bot(id=uuid.uuid4()))
    wrong_app = {"thought": "x", "action": "use_connector", "connector": "jira", "tool": "x"}
    wrong_tool = {"thought": "x", "action": "use_connector", "connector": "tracker",
                  "tool": "delete_everything"}
    model = ScriptedModel([wrong_app, wrong_tool, DONE])
    await _invoke(_Node(_Ctx(), AppGateway(), model, _Org(bots, connectors=FakeApps())))
    assert "no connected app 'jira' (connected: tracker)" in model.prompts[1]
    assert "tracker has no tool 'delete_everything' (its tools: search_issues, create_issue)" in (
        model.prompts[2]
    )
