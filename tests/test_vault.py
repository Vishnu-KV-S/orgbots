"""The login vault: reading a form, sealing what a person types, and the bot never
seeing it.

Most of this needs no database: the form reading and fill planning are pure
(`runtime.domain.vault`), the sealing is `CredentialCipher` with AAD, and the graph runs
against the in-memory fakes from `test_bots.py`. One test drives a real headless
Chromium through `Computer.fill` against a page served from a temp directory, to check
the computer's own last-line checks; it skips if Playwright's browser is not installed.

The thread through all of it: a value a person typed must not appear in anything a
run keeps — the model's prompts, the conversation, a pending action, a request row,
or the browser tool's arguments.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from runtime.computer.browser import _site as computer_site
from runtime.computer.snapshot import render
from runtime.domain.bots import BotRule
from runtime.domain.errors import DecryptionFailed
from runtime.domain.vault import (
    CredentialField,
    VaultOption,
    asked,
    classify,
    form_fields,
    hint,
    lookup,
    plan_fill,
    purpose_of,
    request_id,
    same_site,
    site_of,
    split_values,
    with_next_page,
)
from runtime.gateway.credentials import CredentialCipher
from runtime.gateway.vault import Vault, load_cipher
from runtime.org.bots import vault_option
from runtime.persistence.repositories.vault import VaultEntryRow
from runtime.settings import Settings
from tests.test_bots import (
    FakeBots,
    ScriptedModel,
    _Bot,
    _Ctx,
    _Node,
    _Org,
    _Result,
    _turn,
)

SECRET = "correct-horse-battery-staple"

# --- reading a form ---------------------------------------------------------------------


def _el(n: int, *, tag: str = "input", form: int = 0, **attrs: Any) -> dict[str, Any]:
    return {"id": n, "tag": tag, "form": form, "label": "", "type": "", **attrs}


LOGIN = [
    _el(1, type="search", label="Search", form=0),
    _el(2, type="email", label="Email address", autocomplete="username", form=1),
    _el(3, type="password", label="Password", autocomplete="current-password", form=1),
    _el(4, tag="button", label="Sign in", form=1),
    _el(5, type="checkbox", label="Remember me", form=1),
]


def test_a_login_form_is_read_off_the_page_and_the_search_box_is_not_in_it() -> None:
    fields = form_fields(LOGIN)
    assert [(f.kind, f.elements) for f in fields] == [("email", (2,)), ("password", (3,))]
    assert purpose_of(fields) == "sign_in"


def test_the_anchor_picks_the_form() -> None:
    page = [
        _el(1, type="email", label="Newsletter email", form=0),
        _el(2, type="text", label="Username", form=1),
        _el(3, type="password", label="Password", form=1),
    ]
    assert [f.elements for f in form_fields(page, anchor=3)] == [(2,), (3,)]
    assert [f.elements for f in form_fields(page, anchor=1)] == [(1,)]


def test_a_code_split_into_boxes_is_one_field() -> None:
    page = [_el(i, type="text", inputmode="numeric", maxlength=1, form=-1) for i in range(1, 7)]
    (code,) = form_fields(page)
    assert code.kind == "otp" and code.elements == (1, 2, 3, 4, 5, 6)
    assert purpose_of([code]) == "verify"


def test_a_sign_up_asks_for_the_new_password_once_and_fills_the_confirm_box() -> None:
    page = [
        _el(1, type="text", label="Full name", autocomplete="name", form=-1),
        _el(2, type="email", label="Email", form=-1),
        _el(3, type="password", label="Create password", autocomplete="new-password", form=-1),
        _el(4, type="password", label="Confirm password", autocomplete="new-password", form=-1),
    ]
    fields = form_fields(page)
    assert [f.kind for f in fields] == ["name", "email", "new_password", "confirm_password"]
    assert purpose_of(fields) == "sign_up"
    assert [f.kind for f in asked(fields)] == ["name", "email", "new_password"]

    once, saved = split_values(fields, {"f0": "Ada", "f1": "ada@example.com", "f2": SECRET})
    # The confirm box is answered from the new password; the name is not a credential.
    assert lookup("confirm_password", once) == SECRET
    assert saved == {"email": "ada@example.com", "password": SECRET}


def test_a_two_step_sign_in_asks_for_the_password_on_the_first_page() -> None:
    first = form_fields([_el(1, type="email", label="Email", form=-1)])
    fields = with_next_page(first)
    assert [(f.kind, f.elements) for f in fields] == [("email", (1,)), ("password", ())]


def test_codes_and_payment_fields_are_never_saved_or_read() -> None:
    assert classify(_el(1, type="text", autocomplete="cc-number", label="Card number")) is None
    otp = [CredentialField("f0", "otp", "Code", (1,))]
    once, saved = split_values(otp, {"f0": "123456"})
    assert once == {"otp": "123456"} and saved == {}


@pytest.mark.parametrize(
    ("entry", "page", "same"),
    [
        ("github.com", "github.com", True),
        ("github.com", "www.github.com", True),
        ("accounts.example.com", "example.com", False),
        ("alice.github.io", "mallory.github.io", False),
        ("github.com", "github.com.evil.test", False),
        ("github.com", "", False),
    ],
)
def test_same_site_is_the_same_host(entry: str, page: str, same: bool) -> None:
    assert same_site(entry, page) is same
    # The computer checks the page's host itself, with its own copy of the rule.
    assert (computer_site(entry) == computer_site(page) and bool(page)) is same


def test_the_computer_and_the_domain_agree_on_what_a_site_is() -> None:
    for host in ["WWW.Example.COM.", "www.a.b", "example.com", "", "www."]:
        assert computer_site(host) == site_of(host)


def test_a_hint_names_a_login_without_giving_it_away() -> None:
    assert hint({"email": "ada@example.com", "password": SECRET}) == "a••@example.com"
    assert hint({"phone": "+44 7700 900123"}) == "•••23"
    assert SECRET not in hint({"username": SECRET})


# --- planning a fill --------------------------------------------------------------------


def _opt(kind: str, kinds: set[str], *, host: str = "site.test", auto: bool = True) -> VaultOption:
    return VaultOption(uuid.uuid4(), kind, host, frozenset(kinds), "a••@x", auto)  # type: ignore[arg-type]


FIELDS = [
    CredentialField("f0", "email", "Email", (1,)),
    CredentialField("f1", "password", "Password", (2,)),
]


def test_fresh_values_come_before_a_saved_login() -> None:
    saved = _opt("saved", {"email", "password"})
    once = _opt("once", {"email", "password"})
    plan = plan_fill(FIELDS, [saved, once], tried=set(), host="site.test")
    assert plan is not None and plan.entries == (once.id,)


def test_a_saved_login_is_not_used_automatically_when_the_person_said_not_to() -> None:
    saved = _opt("saved", {"email", "password"}, auto=False)
    assert plan_fill(FIELDS, [saved], tried=set(), host="site.test") is None


def test_a_saved_login_cannot_answer_a_code_or_another_site() -> None:
    code = [CredentialField("f0", "otp", "Code", (1,))]
    assert (
        plan_fill(code, [_opt("saved", {"email", "password"})], tried=set(), host="site.test")
        is None
    )
    other = _opt("saved", {"email", "password"}, host="other.test")
    assert plan_fill(FIELDS, [other], tried=set(), host="site.test") is None


def test_the_same_form_after_a_fill_is_not_filled_again() -> None:
    saved = _opt("saved", {"email", "password"})
    plan = plan_fill(FIELDS, [saved], tried=set(), host="site.test")
    assert plan is not None
    tried = {plan.signature("site.test")}
    assert plan_fill(FIELDS, [saved], tried=tried, host="site.test") is None
    # A second page of the same sign-in is a different form, and is filled.
    password_page = [CredentialField("f0", "password", "Password", (7,))]
    assert plan_fill(password_page, [saved], tried=tried, host="site.test") is not None


# --- sealing ----------------------------------------------------------------------------


def _cipher() -> CredentialCipher:
    return CredentialCipher({"k1": os.urandom(32)}, "k1")


def _row(vault: Vault, org: uuid.UUID, host: str, values: dict[str, str]) -> VaultEntryRow:
    entry = uuid.uuid4()
    key_id, nonce, ct = vault._seal(org, entry, host, values)
    now = dt.datetime.now(dt.UTC)
    return VaultEntryRow(
        entry,
        org,
        host,
        "saved",
        None,
        "",
        tuple(values),
        key_id,
        nonce,
        ct,
        True,
        0,
        None,
        None,
        now,
        now,
    )


def test_an_entry_opens_only_as_the_row_and_site_it_was_sealed_for() -> None:
    vault = Vault(None, _cipher())  # type: ignore[arg-type]
    org = uuid.uuid4()
    row = _row(vault, org, "github.com", {"email": "a@b.c", "password": SECRET})
    assert SECRET.encode() not in row.ciphertext
    assert vault._unseal(row)["password"] == SECRET
    from dataclasses import replace

    with pytest.raises(DecryptionFailed):
        vault._unseal(replace(row, host="evil.test"))
    with pytest.raises(DecryptionFailed):
        vault._unseal(replace(row, organization_id=uuid.uuid4()))
    assert SECRET not in repr(row)


def test_the_vault_key_can_come_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNTIME_CREDENTIAL_KEYS", raising=False)
    key = base64.b64encode(os.urandom(32)).decode()
    cipher = load_cipher(Settings(credential_keys=f"k9:{key}"))
    assert cipher.active_key_id == "k9"


# --- the graph --------------------------------------------------------------------------


@dataclass
class _Request:
    id: uuid.UUID
    host: str
    fields: list[dict[str, Any]]
    status: str = "pending"
    entry_ids: tuple[uuid.UUID, ...] = ()
    saved: bool = False
    working: dict[str, Any] = field(default_factory=dict)


class VaultBots(FakeBots):
    """`FakeBots` plus the vault's metadata calls — options in, cards out, no values."""

    def __init__(self, bot: _Bot, rules: tuple[BotRule, ...] = ()) -> None:
        super().__init__(bot, rules)
        self.options: list[VaultOption] = []
        self.requests: dict[uuid.UUID, _Request] = {}

    async def vault_options(self, bot: Any, host: str) -> list[VaultOption]:
        return [o for o in self.options if same_site(o.host, host)]

    async def request_credentials(
        self,
        bot,
        *,
        run_id,
        step,
        host,
        page_url,
        purpose,  # type: ignore[no-untyped-def]
        fields,
        ask,
        thought,
        saved,
        retry,
        reason,
        working=None,
    ):
        rid = request_id(run_id, step)
        self.requests[rid] = _Request(
            rid,
            host,
            [f.as_dict() | {"ask": f.key in ask} for f in fields],
            working=working or {},
        )
        await self.record(
            bot.id,
            run_id=run_id,
            step=step,
            kind="credentials",
            role="credentials",
            content=thought,
            payload={
                "credential_request_id": str(rid),
                "host": host,
                "purpose": purpose,
                "fields": [{"key": f.key, "kind": f.kind} for f in fields if f.key in ask],
                "saved": [{"id": str(o.id), "label": o.label} for o in saved],
                "retry": retry,
            },
        )
        return rid

    async def credential_request(self, rid: uuid.UUID) -> _Request | None:
        return self.requests.get(rid)


class LoginSite:
    """A sign-in page that stays a sign-in page until it is filled, then a home page."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.url = "about:blank"
        self.accepts = accepts
        self.offset = 0
        """Shift the element numbers, as a page that re-rendered would."""
        self.calls: list[dict[str, Any]] = []

    def _page(self) -> dict[str, Any]:
        elements = (
            [
                _el(1 + self.offset, type="email", label="Email", form=0),
                _el(2 + self.offset, type="password", label="Password", form=0),
                _el(3 + self.offset, tag="button", label="Sign in", form=0),
            ]
            if self.url.endswith("/login")
            else [_el(1, tag="a", label="Account", form=-1)]
        )
        return {
            "ok": True,
            "url": self.url,
            "title": "t",
            "controller": "bot",
            "rendered": f"URL: {self.url}",
            "elements": elements,
        }

    async def execute(self, ctx: Any, call: Any) -> _Result:
        self.calls.append({"tool": call.tool, **call.args})
        if call.tool == "browser.act@1":
            action = call.args["action"]
            if action["type"] == "navigate":
                self.url = action["url"]
            elif action["type"] == "fill_credentials" and self.accepts:
                self.url = "https://site.test/home"
        return _Result(self._page())

    def acts(self) -> list[dict[str, Any]]:
        return [c["action"] for c in self.calls if c["tool"] == "browser.act@1"]


def _everything(bots: VaultBots, model: ScriptedModel, site: LoginSite) -> str:
    """Every string a run kept or showed, in one place to search."""
    return json.dumps(
        {
            "log": [(m.role, m.content, m.payload) for m in bots.log.values()],
            "pending": [p.action for p in bots.pendings.values()],
            "requests": [r.__dict__ for r in bots.requests.values()],
            "prompts": model.prompts,
            "system": model.system,
            "calls": site.calls,
        },
        default=str,
    )


async def test_a_sign_in_page_with_nothing_saved_asks_the_person_with_a_card() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    model = ScriptedModel(
        [
            {"thought": "Open login", "action": "navigate", "url": "https://site.test/login"},
            {"thought": "This needs a login", "action": "sign_in"},
        ]
    )
    out = await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)

    assert out["status"] == "awaiting_credentials"
    (card,) = bots.said("credentials")
    assert card.payload["host"] == "site.test"
    assert [f["kind"] for f in card.payload["fields"]] == ["email", "password"]
    assert [a["type"] for a in site.acts()] == ["navigate"]


async def test_typing_a_password_becomes_a_card_and_the_text_is_dropped_unread() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    model = ScriptedModel(
        [
            {"thought": "Open login", "action": "navigate", "url": "https://site.test/login"},
            {"thought": "Type it", "action": "type", "element": 2, "text": SECRET},
        ]
    )
    out = await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)

    assert out["status"] == "awaiting_credentials"
    assert not bots.pendings, "a password must not be parked in an approval row"
    assert all(a["type"] != "type" for a in site.acts())
    kept = _everything(bots, model, site)
    # The model wrote it (that is the bug this guards against); nothing after it did.
    assert SECRET not in kept.replace(json.dumps(model.prompts), "")
    assert SECRET not in json.dumps([m.payload for m in bots.log.values()])


async def test_answering_the_card_fills_the_form_with_entry_ids_only() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    site.url = "https://site.test/login"
    model = ScriptedModel([{"thought": "Asking", "action": "sign_in"}])
    await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)
    (request,) = bots.requests.values()
    request.working = {"plan": ["sign in", "download invoice"], "notes": "acct 42"}

    # The person submits; the API seals it and marks the request (here, by hand).
    entry = uuid.uuid4()
    request.status, request.entry_ids = "submitted", (entry,)
    model2 = ScriptedModel([{"thought": "In", "action": "reply", "text": "Signed in."}])
    out = await _turn(
        _Node(_Ctx(), site, model2, _Org(bots)), bot.id, resume_credentials_id=str(request.id)
    )

    assert out["status"] == "replied"
    fill = site.acts()[-1]
    assert fill == {
        "type": "fill_credentials",
        "entries": [str(entry)],
        "fields": [
            {"key": "f0", "kind": "email", "label": "Email", "elements": [1]},
            {"key": "f1", "kind": "password", "label": "Password", "elements": [2]},
        ],
        "submit": True,
    }
    # The model saw the outcome — and its working memory survived the new run.
    assert "filled the site.test form" in model2.prompts[0]
    assert "download invoice" in model2.prompts[0]


async def test_a_saved_login_signs_in_without_asking_and_says_which() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    saved = _opt("saved", {"email", "password"})
    bots.options = [saved]
    site = LoginSite()
    site.url = "https://site.test/login"
    model = ScriptedModel(
        [
            {"thought": "Sign in", "action": "sign_in"},
            {"thought": "Done", "action": "reply", "text": "In."},
        ]
    )
    out = await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)

    assert out["status"] == "replied"
    assert not bots.requests
    assert site.acts()[-1]["entries"] == [str(saved.id)]
    assert "saved login a••@x" in model.prompts[1]
    (line,) = [m for m in bots.said("activity") if m.payload["action"]["type"] == "sign_in"]
    assert line.payload["action"]["via"] == "saved"


async def test_a_form_that_comes_back_after_a_fill_is_asked_about_not_refilled() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    bots.options = [_opt("saved", {"email", "password"})]
    site = LoginSite(accepts=False)
    site.url = "https://site.test/login"
    model = ScriptedModel(
        [{"thought": "Sign in", "action": "sign_in"}, {"thought": "Again", "action": "sign_in"}]
    )
    out = await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)

    assert out["status"] == "awaiting_credentials"
    assert sum(a["type"] == "fill_credentials" for a in site.acts()) == 1
    (card,) = bots.said("credentials")
    assert card.payload["retry"] is True
    # The card offers the saved login, so the person can still pick it.
    assert len(card.payload["saved"]) == 1


async def test_an_ask_first_rule_puts_a_saved_login_on_the_card_instead() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot, rules=(BotRule("sign_in", "site.test", "ask"),))
    bots.options = [_opt("saved", {"email", "password"})]
    site = LoginSite()
    site.url = "https://site.test/login"
    model = ScriptedModel([{"thought": "Sign in", "action": "sign_in"}])
    out = await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)

    assert out["status"] == "awaiting_credentials"
    assert all(a["type"] != "fill_credentials" for a in site.acts())


async def test_a_declined_card_is_told_to_the_bot() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    site.url = "https://site.test/login"
    await _turn(
        _Node(_Ctx(), site, ScriptedModel([{"thought": "x", "action": "sign_in"}]), _Org(bots)),
        bot.id,
    )
    (request,) = bots.requests.values()
    request.status = "cancelled"
    model = ScriptedModel([{"thought": "ok", "action": "reply", "text": "Skipped."}])
    await _turn(
        _Node(_Ctx(), site, model, _Org(bots)), bot.id, resume_credentials_id=str(request.id)
    )
    assert "chose NOT to sign in to site.test" in model.prompts[0]


def test_a_vault_row_becomes_an_option_without_a_value() -> None:
    vault = Vault(None, _cipher())  # type: ignore[arg-type]
    row = _row(vault, uuid.uuid4(), "site.test", {"email": "a@b.c", "password": SECRET})
    option = vault_option(row)
    assert option.kinds == {"email", "password"} and SECRET not in repr(option)


# --- the snapshot and the computer ------------------------------------------------------


def test_a_filled_secret_box_renders_as_filled_not_as_its_value() -> None:
    text = render(
        {
            "url": "u",
            "title": "t",
            "elements": [
                {
                    "id": 1,
                    "tag": "input",
                    "type": "password",
                    "label": "Password",
                    "value": "",
                    "filled": True,
                    "in_view": True,
                },
            ],
        }
    )
    assert "(filled)" in text


def _chromium_available() -> bool:
    """A browser Playwright can actually launch: one named in `COMPUTER_CHROMIUM_PATH`,
    or a downloaded build with its executable present (a half-finished download leaves
    the folder without one)."""
    if os.environ.get("COMPUTER_CHROMIUM_PATH"):
        return True
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(cache):
        return False
    return any(
        os.path.isfile(os.path.join(cache, d, sub, exe))
        for d in os.listdir(cache)
        if d.startswith("chromium")
        for sub in ("chrome-linux", "chrome-linux64")
        for exe in ("chrome", "headless_shell")
    )


@pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium installed")
async def test_the_computer_fills_only_the_right_site_and_never_reads_it_back(
    tmp_path: Any,
) -> None:
    import http.server
    import threading

    from runtime.computer.browser import Computer, ComputerError

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<title>Login</title><form action='/done.html'>"
        "<input id=u type=email name=email aria-label=Email>"
        "<input id=p type=password name=password>"
        "<input id=q type=text name=nickname>"
        "<button>Sign in</button></form>"
    )
    (site / "done.html").write_text("<title>Done</title>Welcome")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    computer = Computer(tmp_path / "profile", headless=True)
    try:
        screen = await computer.screen("t")
        await computer.act(screen, {"type": "navigate", "url": url})
        snap = await computer.observe(screen)
        ids = {e["name"]: e["id"] for e in snap["elements"] if e["tag"] == "input"}

        with pytest.raises(ComputerError, match="not a password box"):
            await computer.fill(
                screen,
                expect_host="127.0.0.1",
                fields=[{"elements": [ids["nickname"]], "value": SECRET, "password": True}],
                submit=False,
            )
        with pytest.raises(ComputerError, match="nothing was filled"):
            await computer.fill(
                screen,
                expect_host="evil.test",
                fields=[{"elements": [ids["password"]], "value": SECRET, "password": True}],
                submit=False,
            )

        snap = await computer.fill(
            screen,
            expect_host="127.0.0.1",
            fields=[
                {"elements": [ids["email"]], "value": "ada@example.com", "password": False},
                {"elements": [ids["password"]], "value": SECRET, "password": True},
            ],
            submit=False,
        )
        assert SECRET not in json.dumps(snap) and "ada@example.com" not in json.dumps(snap)
        assert {
            e["name"]: e.get("filled", False) for e in snap["elements"] if e["tag"] == "input"
        } == {
            "email": True,
            "password": True,
            "nickname": False,
        }
        typed = await screen.page.eval_on_selector("#p", "el => el.value")
        assert typed == SECRET
    finally:
        await computer.stop()
        server.shutdown()


# --- the vault in Postgres --------------------------------------------------------------


async def _bot_row(uow_factory: Any, organization_id: Any) -> uuid.UUID:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "vault")
    bot_id = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.bots.create(
            bot_id, organization_id, actor_name=f"bot-v-{bot_id.hex[:6]}", name="Scout"
        )
    return bot_id


LOGIN_FIELDS = [
    CredentialField("f0", "email", "Email", (2,)),
    CredentialField("f1", "password", "Password", (3,)),
]


async def test_a_submission_is_sealed_saved_reused_and_scoped(
    uow_factory: Any, organization_id: Any
) -> None:
    bot_id = await _bot_row(uow_factory, organization_id)
    other_bot = await _bot_row(uow_factory, organization_id)
    vault = Vault(uow_factory, _cipher())

    async with uow_factory.transaction() as uow:
        first = await vault.submit(
            uow,
            organization_id,
            bot_id=bot_id,
            host="www.Site.test",
            fields=LOGIN_FIELDS,
            submitted={"f0": "ada@example.com", "f1": SECRET},
            save=True,
        )
    assert first.saved_id is not None and first.label == "a••@example.com"

    # Nothing in the table is readable without the key.
    async with uow_factory() as uow:
        rows = [await uow.vault.get_entry(first.once_id), await uow.vault.get_entry(first.saved_id)]
        options = await uow.vault.options(organization_id, "site.test", bot_id)
        others = await uow.vault.options(organization_id, "site.test", other_bot)
    assert all(r is not None and SECRET.encode() not in r.ciphertext for r in rows)
    assert {o.kind for o in options} == {"once", "saved"}
    assert [o.kind for o in others] == ["saved"], "a one-time value is the asking bot's alone"
    assert set(rows[1].kinds) == {"email", "password"}  # type: ignore[union-attr]

    site, values = await vault.open(organization_id, [first.once_id], bot_id=bot_id)
    assert site == "site.test" and values["password"] == SECRET
    from runtime.gateway.vault import VaultRefusedError

    with pytest.raises(VaultRefusedError, match="another bot"):
        await vault.open(organization_id, [first.once_id], bot_id=other_bot)
    with pytest.raises(VaultRefusedError, match="does not exist"):
        await vault.open(uuid.uuid4(), [first.saved_id], bot_id=bot_id)

    # The same account again with a new password replaces the saved login, not adds one.
    async with uow_factory.transaction() as uow:
        second = await vault.submit(
            uow,
            organization_id,
            bot_id=bot_id,
            host="site.test",
            fields=LOGIN_FIELDS,
            submitted={"f0": "ADA@example.com", "f1": "a-newer-password"},
            save=True,
        )
    assert second.saved_id == first.saved_id
    _, values = await vault.open(organization_id, [first.saved_id], bot_id=other_bot)
    assert values["password"] == "a-newer-password"
    async with uow_factory() as uow:
        saved = await uow.vault.saved(organization_id)
    assert [e.use_count for e in saved] == [1], "a refused open is not a use"


async def test_a_request_is_answered_once_and_holds_no_value(
    uow_factory: Any, organization_id: Any
) -> None:
    bot_id = await _bot_row(uow_factory, organization_id)
    rid = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.vault.add_request(
            rid,
            bot_id,
            run_id=None,
            host="site.test",
            page_url="https://site.test/login",
            purpose="sign_in",
            fields=[f.as_dict() | {"ask": True} for f in LOGIN_FIELDS],
            working={"plan": ["sign in"]},
        )
        assert [r.id for r in await uow.vault.live_requests(bot_id)] == [rid]
        entry = uuid.uuid4()
        assert await uow.vault.decide_request(rid, status="submitted", entry_ids=[entry])
        assert not await uow.vault.decide_request(rid, status="cancelled")
        request = await uow.vault.get_request(rid)
    assert request is not None and request.entry_ids == (entry,)
    assert request.working == {"plan": ["sign in"]}
    assert CredentialField.from_dict(request.fields[1]) == LOGIN_FIELDS[1]


async def test_an_expired_one_time_value_is_refused_and_purged(
    uow_factory: Any, organization_id: Any
) -> None:
    from sqlalchemy import text

    from runtime.gateway.vault import VaultRefusedError

    bot_id = await _bot_row(uow_factory, organization_id)
    vault = Vault(uow_factory, _cipher())
    code = [CredentialField("f0", "otp", "Code", (1,))]
    async with uow_factory.transaction() as uow:
        sealed = await vault.submit(
            uow,
            organization_id,
            bot_id=bot_id,
            host="site.test",
            fields=code,
            submitted={"f0": "123456"},
            save=True,
        )
        assert sealed.saved_id is None, "a code is never saved"
        await uow.session.execute(
            text(
                "UPDATE vault_entries SET expires_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": sealed.once_id},
        )
    with pytest.raises(VaultRefusedError, match="expired"):
        await vault.open(organization_id, [sealed.once_id], bot_id=bot_id)
    async with uow_factory.transaction() as uow:
        assert await uow.vault.purge_expired() == 1


# --- the browser tool's fill ------------------------------------------------------------


class _OneVault:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.opened: list[tuple[Any, list[uuid.UUID], uuid.UUID]] = []

    async def open(self, org: Any, entries: list[uuid.UUID], *, bot_id: uuid.UUID, profile: str = ""):  # type: ignore[no-untyped-def]  # noqa: E501
        self.opened.append((org, entries, bot_id))
        return "site.test", dict(self.values)


async def _run_fill(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[int, dict[str, Any]], vault: _OneVault
) -> tuple[Any, list[dict[str, Any]]]:
    import httpx

    from runtime.gateway.builtin import browser
    from runtime.gateway.tools import ToolContext

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, **json.loads(request.content)})
        return httpx.Response(answer[0], json=answer[1])

    real = httpx.AsyncClient
    monkeypatch.setattr(
        browser.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    fill = browser._filler("http://computer", lambda: vault)  # type: ignore[arg-type,return-value]
    bot_id = uuid.uuid4()
    ctx = ToolContext(
        run_id="r",
        organization_id=str(uuid.uuid4()),
        logical_call_id="c",
        idempotency_key="k",
        marker="m",
        attempt=1,
    )
    args = browser.ActArgs(
        screen_id=str(bot_id),
        action={
            "type": "fill_credentials",
            "entries": [str(uuid.uuid4())],
            "fields": [
                {"key": "f0", "kind": "username", "elements": [2]},
                {"key": "f1", "kind": "password", "elements": [3]},
                {"key": "f2", "kind": "password", "elements": []},
            ],
            "submit": True,
        },
    )
    return await fill(ctx, args), sent


async def test_the_tool_opens_the_vault_and_sends_values_only_to_the_computer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _OneVault({"email": "ada@example.com", "password": SECRET})
    result, sent = await _run_fill(
        monkeypatch, (200, {"ok": True, "url": "https://site.test/home"}), vault
    )
    (request,) = sent
    assert request["path"].endswith("/fill") and request["expect_host"] == "site.test"
    # A username box takes the email; the password goes only where a password goes;
    # a field with no element (the next page's) is not sent.
    assert request["fields"] == [
        {"elements": [2], "value": "ada@example.com", "password": False},
        {"elements": [3], "value": SECRET, "password": True},
    ]
    assert result.ok and SECRET not in result.model_dump_json()


async def test_a_validation_error_from_the_computer_is_not_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _OneVault({"email": "ada@example.com", "password": SECRET})
    echoed = {"detail": [{"loc": ["body"], "input": {"value": SECRET}}]}
    result, _ = await _run_fill(monkeypatch, (422, echoed), vault)
    assert not result.ok and SECRET not in (result.error or "")


async def test_a_field_the_vault_cannot_answer_fills_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, sent = await _run_fill(monkeypatch, (200, {}), _OneVault({"email": "a@b.c"}))
    assert sent == [] and "no password" in (result.error or "")


async def _answered(bots: VaultBots, site: LoginSite, bot: _Bot) -> _Request:
    await _turn(
        _Node(_Ctx(), site, ScriptedModel([{"thought": "x", "action": "sign_in"}]), _Org(bots)),
        bot.id,
    )
    (request,) = bots.requests.values()
    request.status, request.entry_ids = "submitted", (uuid.uuid4(),)
    return request


async def test_a_resumed_fill_finds_the_form_again_on_the_page_as_it_is_now() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    site.url = "https://site.test/login"
    request = await _answered(bots, site, bot)
    site.offset = 10  # the page re-rendered while the person was typing
    model = ScriptedModel([{"thought": "In", "action": "reply", "text": "ok"}])
    await _turn(
        _Node(_Ctx(), site, model, _Org(bots)), bot.id, resume_credentials_id=str(request.id)
    )
    fill = site.acts()[-1]
    assert [(f["kind"], f["elements"]) for f in fill["fields"]] == [
        ("email", [11]),
        ("password", [12]),
    ]


async def test_a_resumed_fill_on_a_page_without_the_form_fills_nothing_and_says_so() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = LoginSite()
    site.url = "https://site.test/login"
    request = await _answered(bots, site, bot)
    site.url = "https://site.test/home"  # the person signed in by hand meanwhile
    model = ScriptedModel([{"thought": "ok", "action": "reply", "text": "ok"}])
    await _turn(
        _Node(_Ctx(), site, model, _Org(bots)), bot.id, resume_credentials_id=str(request.id)
    )
    assert all(a["type"] != "fill_credentials" for a in site.acts())
    assert "no longer on the screen" in model.prompts[0]
