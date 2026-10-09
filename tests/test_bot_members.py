"""Members: sign-in, roles, whose bots are whose, and single sign-on.

The API runs with `RUNTIME_AUTH_MODE=members` against `runtime_features_test`; each
person is an httpx client holding their own session cookie. Single sign-on talks to a
fake identity provider through the OIDC client's transport: it checks the PKCE verifier
and the client's credentials like a real one and signs real RS256 ID tokens, so the
verification is the real verification. Browser-profile isolation is checked in real
Chrome.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.members import (
    Member,
    MemberError,
    NotAllowedError,
    can_edit,
    can_see,
    check_id_claims,
    check_role_change,
    computer_profile,
)
from runtime.gateway.oidc import OIDCClient

UI = "http://ui.test"


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_CREDENTIAL_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode())


# --- the rules, without a database -------------------------------------------------------


@dataclass
class _Bot:
    organization_id: uuid.UUID
    owner_member_id: uuid.UUID | None
    visibility: str = "private"
    team_id: uuid.UUID = field(default_factory=uuid.uuid4)


def _member(org: uuid.UUID, role: str = "member") -> Member:
    return Member(id=uuid.uuid4(), organization_id=org, email="a@b.test", name="", role=role)  # type: ignore[arg-type]


def test_a_private_bot_is_its_owners_and_a_team_bot_everyones_to_use() -> None:
    org = uuid.uuid4()
    olive, bob, admin = _member(org), _member(org), _member(org, "admin")
    private = _Bot(org, olive.id)
    team = _Bot(org, olive.id, "team")
    nobodys = _Bot(org, None)
    elsewhere = _Bot(uuid.uuid4(), None)
    assert can_see(olive, private) and not can_see(bob, private) and not can_see(admin, private)
    assert can_see(bob, team) and not can_edit(bob, team) and can_edit(admin, team)
    assert can_see(bob, nobodys) and can_edit(bob, nobodys)
    assert not can_see(olive, elsewhere)
    assert can_see(None, elsewhere), "without members everything is the one person's"
    assert computer_profile(private) == f"m-{olive.id.hex}"
    assert computer_profile(team) == f"t-{team.team_id.hex}"
    assert computer_profile(nobodys) == ""


def test_roles_keep_an_owner_and_keep_owners_to_owners() -> None:
    org = uuid.uuid4()
    owner, admin, member = _member(org, "owner"), _member(org, "admin"), _member(org)
    check_role_change(admin, "member", "admin", owners=1)
    with pytest.raises(NotAllowedError):
        check_role_change(admin, "member", "owner", owners=1)
    with pytest.raises(NotAllowedError):
        check_role_change(member, "member", "admin", owners=1)
    with pytest.raises(MemberError, match="at least one owner"):
        check_role_change(owner, "owner", "admin", owners=1)
    check_role_change(owner, "owner", "admin", owners=2)


def test_an_id_token_must_be_for_this_client_this_sign_in_and_a_verified_email() -> None:
    now = dt.datetime.now(dt.UTC)
    good = {"iss": "https://idp.test", "aud": "app", "nonce": "n1", "email": "A@Acme.test"}
    kw: dict[str, Any] = {"issuer": "https://idp.test", "client_id": "app", "nonce": "n1"}
    assert check_id_claims(good, now=now, **kw) == "a@acme.test"
    assert check_id_claims({**good, "aud": ["x", "app"]}, now=now, **kw) == "a@acme.test"
    for bad, says in [
        ({"iss": "https://evil.test"}, "another issuer"),
        ({"aud": "other"}, "another app"),
        ({"nonce": "n2"}, "start again"),
        ({"email": None}, "no email"),
        ({"email_verified": False}, "not verified"),
    ]:
        with pytest.raises(MemberError, match=says):
            check_id_claims({**good, **bad}, now=now, **kw)


# --- the API, with members -----------------------------------------------------------------


@dataclass
class Team:
    app: Any
    settings: Any
    org: uuid.UUID
    service: Any
    owner: httpx.AsyncClient
    clients: list[httpx.AsyncClient]

    def anonymous(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=UI)
        self.clients.append(client)
        return client

    async def sign_in(self, link: str, name: str = "") -> httpx.AsyncClient:
        client = self.anonymous()
        token = link.rsplit("link=", 1)[1]
        joined = await client.post(f"/v1/auth/links/{token}", json={"name": name})
        assert joined.status_code == 200, joined.text
        return client

    async def invite(self, email: str, name: str, role: str = "member") -> httpx.AsyncClient:
        made = await self.owner.post("/v1/members/invites", json={"email": email, "role": role})
        assert made.status_code == 201, made.text
        return await self.sign_in(made.json()["link"], name)


@pytest_asyncio.fixture
async def team(settings: Any, uow_factory: Any, organization_id: Any) -> AsyncIterator[Team]:
    from runtime.runtime.bootstrap import Registrar
    from runtime.runtime.members import MemberService
    from runtime.runtime.run_service import RunService

    members = settings.model_copy(
        update={
            "auth_mode": "members",
            "bots_organization_id": str(organization_id),
            "ui_url": UI,
        }
    )
    app = create_app(members)
    app.state.settings = members
    app.state.uow = uow_factory
    app.state.service = RunService(uow_factory, settings=members)
    await Registrar(uow_factory).ensure_organization(organization_id, "Acme")
    service = MemberService(uow_factory, members)
    app.state.members = service
    _, token = await service.add_owner(organization_id, "olive@acme.test", name="Olive")
    t = Team(app, members, organization_id, service, owner=None, clients=[])  # type: ignore[arg-type]
    t.owner = await t.sign_in(f"{UI}/signin?link={token}")
    try:
        yield t
    finally:
        for c in t.clients:
            await c.aclose()


async def test_nothing_but_signing_in_answers_without_a_session(team: Team) -> None:
    stranger = team.anonymous()
    for path in (
        "/v1/bots",
        "/v1/groups",
        "/v1/vault",
        "/v1/computer/workspace",
        "/v1/skills",
        "/v1/connectors",
        "/v1/members",
        "/v1/templates/preview",
        "/v1/observe/organizations",
    ):
        method = "POST" if path.endswith("preview") else "GET"
        got = await stranger.request(method, path, json={} if method == "POST" else None)
        assert got.status_code == 401, (path, got.status_code)
    me = (await stranger.get("/v1/auth/me")).json()
    assert me == {"mode": "members", "member": None}
    owner_me = (await team.owner.get("/v1/auth/me")).json()["member"]
    assert owner_me["email"] == "olive@acme.test" and owner_me["role"] == "owner"


async def test_a_members_bots_are_private_until_shared_and_then_only_the_owner_sets_them_up(
    team: Team, uow_factory: Any
) -> None:
    bob = await team.invite("bob@acme.test", "Bob")
    mine = (await team.owner.post("/v1/bots", json={"name": "Scout"})).json()
    assert mine["visibility"] == "private" and mine["owner_member_id"]
    assert (await bob.get(f"/v1/bots/{mine['id']}")).status_code == 404
    assert [b["name"] for b in (await bob.get("/v1/bots")).json()["bots"]] == []
    found = (await bob.get("/v1/bots/search", params={"q": "Scout"})).json()
    assert found["bots"] == []

    shared = await team.owner.patch(f"/v1/bots/{mine['id']}", json={"visibility": "team"})
    assert shared.json()["visibility"] == "team"
    assert [b["name"] for b in (await bob.get("/v1/bots")).json()["bots"]] == ["Scout"]

    sent = await bob.post(f"/v1/bots/{mine['id']}/messages", json={"text": "hello"})
    assert sent.status_code == 202, sent.text
    said = (await team.owner.get(f"/v1/bots/{mine['id']}/messages")).json()["messages"]
    assert said[-1]["payload"]["from"]["name"] == "Bob", "a team bot shows who said it"

    for method, path, body in [
        ("PATCH", "", {"name": "Bob's now"}),
        ("POST", "/rules", {"action_type": "click", "host": "", "decision": "allow"}),
        ("POST", "/memories", {"content": "Bob is the boss"}),
        ("POST", "/routines", {"name": "R", "instruction": "x", "cron": "0 8 * * *"}),
        ("POST", "/template-links", None),
        ("DELETE", "", None),
    ]:
        got = await bob.request(method, f"/v1/bots/{mine['id']}{path}", json=body)
        assert got.status_code == 403, (method, path, got.status_code)
        assert "only its owner or an admin" in got.json()["detail"]
    copy = await bob.post(f"/v1/bots/{mine['id']}/duplicate")
    assert copy.status_code == 201 and copy.json()["visibility"] == "private"
    assert copy.json()["owner_member_id"] != mine["owner_member_id"], "a copy is the copier's"

    pid = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.bots.add_pending(
            pid, uuid.UUID(mine["id"]), run_id=None, action={"type": "click"}, reason="buys"
        )
    always = await bob.post(f"/v1/bots/{mine['id']}/pending/{pid}", json={"decision": "always"})
    assert always.status_code == 403 and "allow it once" in always.json()["detail"]
    once = await bob.post(f"/v1/bots/{mine['id']}/pending/{pid}", json={"decision": "once"})
    assert once.status_code == 202, once.text


async def test_roles_links_and_suspension(team: Team, uow_factory: Any) -> None:
    alice = await team.invite("alice@acme.test", "Alice", role="admin")
    bob = await team.invite("bob@acme.test", "Bob")
    assert (await bob.post("/v1/members/invites", json={"email": "c@acme.test"})).status_code == 403
    owner_role = await alice.post(
        "/v1/members/invites", json={"email": "c@acme.test", "role": "owner"}
    )
    assert owner_role.status_code == 403

    listed = (await bob.get("/v1/members")).json()["members"]
    by_email = {m["email"]: m for m in listed}
    assert set(by_email) == {"olive@acme.test", "alice@acme.test", "bob@acme.test"}
    olive = by_email["olive@acme.test"]["id"]
    last = await team.owner.patch(f"/v1/members/{olive}", json={"role": "admin"})
    assert last.status_code == 422 and "at least one owner" in last.json()["detail"]

    link = (await alice.post(f"/v1/members/{by_email['bob@acme.test']['id']}/sign-in-link")).json()
    second_bob = await team.sign_in(link["link"])
    token = link["link"].rsplit("link=", 1)[1]
    again = await team.anonymous().post(f"/v1/auth/links/{token}", json={})
    assert again.status_code == 410 and "already used" in again.json()["detail"]

    gone = await alice.patch(
        f"/v1/members/{by_email['bob@acme.test']['id']}", json={"active": False}
    )
    assert gone.status_code == 200 and gone.json()["active"] is False
    assert (await bob.get("/v1/bots")).status_code == 401
    assert (await second_bob.get("/v1/bots")).status_code == 401, "every session ends"

    invite = (await team.owner.post("/v1/members/invites", json={"email": "d@acme.test"})).json()
    async with uow_factory.transaction() as uow:
        await uow.session.execute(text("UPDATE member_links SET expires_at = now()"))
    expired = await team.anonymous().get(f"/v1/auth/links/{invite['link'].rsplit('=', 1)[1]}")
    assert expired.status_code == 410 and "expired" in expired.json()["detail"]


async def test_a_member_never_chooses_their_organization_and_the_console_is_for_admins(
    team: Team,
) -> None:
    bob = await team.invite("bob@acme.test", "Bob")
    other = str(uuid.uuid4())
    made = await bob.post("/v1/bots", json={"name": "Mine"}, headers={ORG_HEADER: other})
    listed = await bob.get("/v1/bots", headers={ORG_HEADER: other})
    assert listed.json()["organization_id"] == str(team.org), "the header is ignored"
    assert [b["id"] for b in listed.json()["bots"]] == [made.json()["id"]]
    assert (await bob.get("/v1/observe/organizations")).status_code == 403
    assert (await team.owner.get("/v1/observe/organizations")).status_code == 200
    reset = await bob.post("/v1/computer/reset")
    assert reset.status_code == 403


async def test_a_group_is_its_creators_and_holds_only_bots_they_can_see(team: Team) -> None:
    bob = await team.invite("bob@acme.test", "Bob")
    a = (await team.owner.post("/v1/bots", json={"name": "A"})).json()
    b = (await team.owner.post("/v1/bots", json={"name": "B"})).json()
    group = await team.owner.post(
        "/v1/groups", json={"name": "Desk", "members": [a["id"], b["id"]]}
    )
    assert group.status_code == 201, group.text
    gid = group.json()["id"]
    assert (await bob.get(f"/v1/groups/{gid}")).status_code == 404
    assert (await bob.get("/v1/groups")).json()["groups"] == []
    own = (await bob.post("/v1/bots", json={"name": "Bee"})).json()
    sneaky = await bob.post("/v1/groups", json={"name": "Mine", "members": [own["id"], a["id"]]})
    assert sneaky.status_code == 422, "not someone else's private bot"


async def test_push_goes_to_whoever_last_spoke_to_the_bot(team: Team, uow_factory: Any) -> None:
    from runtime.org.bots import BotService

    bob = await team.invite("bob@acme.test", "Bob")
    bot = (await team.owner.post("/v1/bots", json={"name": "Scout"})).json()
    await team.owner.patch(f"/v1/bots/{bot['id']}", json={"visibility": "team"})
    await bob.post(f"/v1/bots/{bot['id']}/messages", json={"text": "find a mug"})
    bob_id = next(
        m["id"] for m in (await bob.get("/v1/members")).json()["members"] if m["name"] == "Bob"
    )
    async with uow_factory() as uow:
        row = await uow.bots.get(uuid.UUID(bot["id"]))
    await BotService(uow_factory).notify(row, "reply", "Found one", run_id=uuid.uuid4(), step=1)
    async with uow_factory() as uow:
        (note,) = await uow.push.unsent()
    assert str(note.member_id) == bob_id


async def test_saved_logins_belong_to_one_browser_profile(
    team: Team, uow_factory: Any, keys: None
) -> None:
    from runtime.domain.vault import CredentialField
    from runtime.gateway.vault import Vault, VaultRefusedError

    vault = Vault.from_settings(uow_factory, team.settings)
    mine = (await team.owner.post("/v1/bots", json={"name": "Mine"})).json()
    bob = await team.invite("bob@acme.test", "Bob")
    theirs = (await bob.post("/v1/bots", json={"name": "Theirs"})).json()
    async with uow_factory() as uow:
        mine_row = await uow.bots.get(uuid.UUID(mine["id"]))
        theirs_row = await uow.bots.get(uuid.UUID(theirs["id"]))
    async with uow_factory.transaction() as uow:
        sealed = await vault.submit(
            uow,
            team.org,
            bot_id=mine_row.id,
            host="shop.test",
            fields=[CredentialField("f0", "password", "Password", (1,))],
            submitted={"f0": "olives-password"},
            save=True,
            profile=computer_profile(mine_row),
        )
    assert sealed.saved_id is not None
    async with uow_factory() as uow:
        offered = await uow.vault.options(
            team.org, "shop.test", theirs_row.id, computer_profile(theirs_row)
        )
    assert offered == [], "Bob's bot is not offered Olive's login"
    with pytest.raises(VaultRefusedError):
        await vault.open(
            team.org, [sealed.saved_id], bot_id=theirs_row.id, profile=computer_profile(theirs_row)
        )
    _, values = await vault.open(
        team.org, [sealed.saved_id], bot_id=mine_row.id, profile=computer_profile(mine_row)
    )
    assert "olives-password" in values.values()
    assert [e["host"] for e in (await team.owner.get("/v1/vault")).json()["entries"]] == [
        "shop.test"
    ]
    assert (await bob.get("/v1/vault")).json()["entries"] == []


# --- single sign-on --------------------------------------------------------------------------


class FakeIdP:
    """An OpenID provider: discovery, keys, and a token endpoint that checks PKCE and
    the client's secret, then signs an ID token for whoever `authorize` was told."""

    issuer = "https://idp.test"

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict[str, Any]] = {}
        self.secret = "s3cret"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token",
                    "jwks_uri": self.issuer + "/jwks",
                },
            )
        if path == "/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig"}]})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            expected = base64.b64encode(f"acme-app:{self.secret}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "invalid_client"})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "PKCE"}
                )
            now = int(dt.datetime.now(dt.UTC).timestamp())
            claims = {
                "iss": self.issuer,
                "aud": "acme-app",
                "sub": grant["email"],
                "iat": now,
                "exp": now + 300,
                "nonce": grant["nonce"],
                "email": grant["email"],
                "email_verified": True,
                "name": grant["name"],
                **grant["extra"],
            }
            key = grant["sign_with"] or self.key
            token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(
                200, json={"id_token": token, "token_type": "Bearer", "access_token": "at"}
            )
        return httpx.Response(404)

    def authorize(
        self, url: str, *, email: str, name: str = "", sign_with: Any = None, **extra: Any
    ) -> tuple[str, str]:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert query["client_id"] == "acme-app" and query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == f"{UI}/rt/v1/auth/sso/callback"
        code = uuid.uuid4().hex
        self.codes[code] = {
            "nonce": query["nonce"],
            "challenge": query["code_challenge"],
            "email": email,
            "name": name,
            "sign_with": sign_with,
            "extra": extra,
        }
        return code, query["state"]


@pytest_asyncio.fixture
async def sso(team: Team, uow_factory: Any, keys: None) -> AsyncIterator[FakeIdP]:
    from runtime.runtime.members import MemberService

    idp = FakeIdP()
    team.app.state.members = MemberService(
        uow_factory, team.settings, oidc=OIDCClient(transport=httpx.MockTransport(idp.handler))
    )
    saved = await team.owner.put(
        "/v1/members/sso",
        json={
            "issuer": idp.issuer,
            "client_id": "acme-app",
            "client_secret": idp.secret,
            "domains": ["acme.test"],
            "auto_join": False,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_secret"] is True and idp.secret not in saved.text
    yield idp


async def _sso(
    team: Team, idp: FakeIdP, email: str, *, return_to: str = "/", who: str | None = None, **kw: Any
) -> tuple[httpx.AsyncClient, httpx.Response]:
    browser = team.anonymous()
    started = await browser.post(
        "/v1/auth/sso/start", json={"email": email, "return_to": return_to}
    )
    assert started.status_code == 200, started.text
    code, state = idp.authorize(started.json()["redirect_url"], email=who or email, **kw)
    back = await browser.get("/v1/auth/sso/callback", params={"code": code, "state": state})
    return browser, back


async def test_single_sign_on_lets_in_members_and_with_auto_join_their_colleagues(
    team: Team, sso: FakeIdP
) -> None:
    _, refused = await _sso(team, sso, "alice@acme.test", name="Alice")
    assert refused.status_code == 303
    assert "isn%27t%20a%20member" in refused.headers["location"]

    await team.owner.put(
        "/v1/members/sso",
        json={
            "issuer": sso.issuer,
            "client_id": "acme-app",
            "domains": ["acme.test"],
            "auto_join": True,
        },
    )
    alice, back = await _sso(team, sso, "alice@acme.test", name="Alice", return_to="/?bot=x")
    assert back.status_code == 303 and back.headers["location"] == "/?bot=x"
    assert "HttpOnly" in back.headers["set-cookie"] and "SameSite=lax" in back.headers["set-cookie"]
    me = (await alice.get("/v1/auth/me")).json()["member"]
    assert (me["email"], me["name"], me["role"]) == ("alice@acme.test", "Alice", "member")

    _, olive = await _sso(team, sso, "olive@acme.test")
    assert olive.headers["location"] == "/"
    _, away = await _sso(team, sso, "alice@acme.test", return_to="//evil.example/x")
    assert away.headers["location"] == "/", "never an open redirect"


async def test_single_sign_on_refuses_what_it_cannot_trust(team: Team, sso: FakeIdP) -> None:
    async def failure(**kw: Any) -> str:
        _, back = await _sso(team, sso, "olive@acme.test", **kw)
        assert back.status_code == 303 and back.headers["location"].startswith("/signin?error=")
        assert "set-cookie" not in back.headers
        return back.headers["location"]

    assert "start%20again" in await failure(nonce="someone-elses")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert "did%20not%20verify" in await failure(sign_with=other_key)
    assert "isn%27t%20in%20a%20domain" in await failure(who="mallory@evil.test")

    browser = team.anonymous()
    started = await browser.post("/v1/auth/sso/start", json={"email": "olive@acme.test"})
    code, state = sso.authorize(started.json()["redirect_url"], email="olive@acme.test")
    first = await browser.get("/v1/auth/sso/callback", params={"code": code, "state": state})
    assert first.headers["location"] == "/"
    code2, _ = sso.authorize(started.json()["redirect_url"], email="olive@acme.test")
    replay = await browser.get("/v1/auth/sso/callback", params={"code": code2, "state": state})
    assert "already%20used" in replay.headers["location"], "a state is good once"

    nowhere = await team.anonymous().post("/v1/auth/sso/start", json={"email": "x@evil.test"})
    assert nowhere.status_code == 422 and "sign-in link" in nowhere.json()["detail"]


# --- browser profiles ------------------------------------------------------------------------


async def test_two_profiles_do_not_share_a_signed_in_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site's cookie set in one member's profile is not there in another's — the
    reason a teammate's bot is never signed in as you."""
    import http.server
    import threading

    from runtime.computer.app import create_app as computer_app

    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = f"<p id=c>{self.headers.get('cookie') or 'none'}</p>".encode()
            self.send_response(200)
            if self.path == "/login":
                self.send_header("Set-Cookie", "session=olive; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_address[1]}"
    olive, bob = f"m-{uuid.uuid4().hex}", f"m-{uuid.uuid4().hex}"
    app = computer_app(tmp_path / "profile", workspace=tmp_path / "ws")
    await app.state.computer.start()

    async def seen(http_: httpx.AsyncClient, screen: str, profile: str, path: str) -> str:
        await http_.post(
            f"/screens/{screen}/act",
            params={"profile": profile},
            json={"action": {"type": "navigate", "url": site + path}},
        )
        page = (
            await http_.post(f"/screens/{screen}/observe", params={"profile": profile}, json={})
        ).json()
        return page["rendered"]

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as http_:
            await seen(http_, "a", olive, "/login")
            assert "session=olive" in await seen(http_, "b", olive, "/")
            assert "session=olive" not in await seen(http_, "c", bob, "/")
            assert "session=olive" not in await seen(http_, "d", "", "/")
            moved = await seen(http_, "a", bob, "/")
            assert "session=olive" not in moved, "a screen that changes profile moves"
            bad = await http_.post("/screens/x/observe", params={"profile": "../etc"}, json={})
            assert bad.status_code == 422
            written = await http_.post(
                "/workspace/file",
                params={"profile": olive},
                json={"path": "/workspace/note.txt", "data": base64.b64encode(b"hi").decode()},
            )
            assert written.status_code == 200
            mine = await http_.get("/workspace", params={"profile": olive})
            other = await http_.get("/workspace", params={"profile": bob})
            default = await http_.get("/workspace")
    finally:
        await app.state.computer.stop()
        server.shutdown()
    assert [e["name"] for e in mine.json()["entries"] if not e["folder"]] == ["note.txt"]
    assert all(e["name"] != "note.txt" for e in other.json()["entries"])
    assert all(e["name"] != "note.txt" for e in default.json()["entries"])
    assert not (tmp_path / "ws" / "note.txt").exists(), "not inside the default workspace"
