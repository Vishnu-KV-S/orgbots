"""Team files: a shared drive for a bot and its helpers.

Four layers, in the order a failure would want to be read:

- the rules, pure (`domain.files`): paths, edits, reading a long file a page at a time,
  and when a whole-file write is safe;
- the drive on Postgres (`org.files.TeamDrive`): one drive per team, revisions,
  idempotent replays, case-insensitive paths, the trash, locks, and what happens to a
  team's files when its bots are deleted;
- `bot_agent@1` against the real drive and the in-memory fakes from `test_bots.py`: a
  lead writes and its helper reads, and a bot cannot overwrite a file it has not read
  or that a teammate changed after it read it;
- the person's surface, `/v1/bots/{id}/files`.

Postgres is `runtime_test`, never the dev database: the suite truncates what it runs on.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.bots import BotStep
from runtime.domain.files import (
    PERSON,
    Editor,
    FileEditError,
    FileLockedError,
    FilePathError,
    FileTakenError,
    StaleFileError,
    Team,
    appended,
    apply_edit,
    check_base,
    move_target,
    normalize_path,
    render_read,
    score,
    team_of,
    window,
)
from runtime.org.bots import BotService
from runtime.org.files import TeamDrive
from runtime.org.killswitch import KillSwitchService
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.bots import BotManager
from runtime.runtime.run_service import RunService
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org, _turn

# --- the rules ---------------------------------------------------------------------------


def test_a_path_means_one_thing_whoever_writes_it() -> None:
    assert normalize_path("notes.md") == "/notes.md"
    assert normalize_path("  Projects\\Acme//vendors.csv ") == "/Projects/Acme/vendors.csv"
    assert normalize_path("/", folder=True) == "/"
    assert normalize_path("/research/", folder=True) == "/research"
    for bad in ("", "/", "/research/", "/a/../b.md", "/a/./b.md", "/what?.md", "/a\x00b"):
        with pytest.raises(FilePathError):
            normalize_path(bad)
    with pytest.raises(FilePathError, match="deep"):
        normalize_path("/" + "/".join("abcdefghij") + ".md")


def test_moving_into_a_folder_keeps_the_name() -> None:
    assert move_target("/drafts/report.md", "/archive/") == "/archive/report.md"
    assert move_target("/drafts/report.md", "/") == "/report.md"
    assert move_target("/drafts/report.md", "final.md") == "/final.md"


def test_an_edit_lands_in_exactly_one_place() -> None:
    assert apply_edit("a\nb\nc", "b", "B") == "a\nB\nc"
    with pytest.raises(FileEditError, match="not in the file"):
        apply_edit("a b c", "x", "y")
    with pytest.raises(FileEditError, match="occurs 2 times"):
        apply_edit("row\nrow", "row", "x")


def test_an_append_starts_a_line_of_its_own() -> None:
    assert appended("", "x") == "x"
    assert appended("a,b", "c,d") == "a,b\nc,d"
    assert appended("a,b\n", "c,d") == "a,b\nc,d"


def test_a_long_file_is_read_a_page_at_a_time_and_fenced_as_data() -> None:
    content = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(1, 201))
    first = window(content, 1, max_chars=1_000)
    assert first.first == 1 and first.last == 9 and first.total == 200
    assert not first.complete
    later = window(content, first.last + 1, max_chars=1_000)
    assert later.text.startswith("line 0010")

    big = _record(
        path="/big.md",
        chars=len(content),
        version=2,
        updated_by_kind="bot",
        updated_by_bot_id=uuid.uuid4(),
        updated_by_name="Scout",
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    shown = render_read(big, first, viewer=None, now=datetime.now(UTC))
    assert "<<<FILE (untrusted content" in shown and shown.count("FILE>>>") == 1
    assert "from_line 10 for more" in shown
    assert "changed by Scout 5m ago" in shown


def test_a_whole_file_write_must_name_the_version_it_replaces() -> None:
    now = datetime.now(UTC)
    plan = _record(path="/plan.md", version=3, updated_by_kind="person", updated_at=now)
    check_base(None, "/plan.md", 0, viewer=None, now=now)  # a new file
    check_base(plan, "/plan.md", 3, viewer=None, now=now)  # the version it read
    check_base(plan, "/plan.md", None, viewer=None, now=now)  # the person, deliberately
    with pytest.raises(StaleFileError, match="read_file it first"):
        check_base(plan, "/plan.md", 0, viewer=None, now=now)
    with pytest.raises(StaleFileError, match="you read v2; it is now v3, changed by your person"):
        check_base(plan, "/plan.md", 2, viewer=None, now=now)


def test_a_search_ranks_files_that_match_more_of_the_words_first() -> None:
    wanted = ["acme", "price"]
    both = score("/vendors.csv", "Acme widget, price 4", wanted)
    one = score("/acme-notes.md", "Acme Acme Acme", wanted)
    assert both > one > 0
    assert score("/x.md", "nothing here", wanted) == 0


def test_a_file_step_needs_the_fields_its_action_needs() -> None:
    with pytest.raises(ValueError, match="path"):
        BotStep(thought="t", action="read_file")
    with pytest.raises(ValueError, match="text"):
        BotStep(thought="t", action="write_file", path="/a.md")
    with pytest.raises(ValueError, match="find"):
        BotStep(thought="t", action="edit_file", path="/a.md", text="x")
    with pytest.raises(ValueError, match="to"):
        BotStep(thought="t", action="move_file", path="/a.md")
    BotStep(thought="t", action="list_files")  # the whole drive
    BotStep(thought="t", action="write_file", path="/empty.md", text="")


# --- the drive, on Postgres ----------------------------------------------------------------


@pytest_asyncio.fixture
async def org(uow_factory: Any, organization_id: Any) -> Any:
    await Registrar(uow_factory).ensure_organization(organization_id, "files")
    return organization_id


def _bot_editor(name: str = "Scout") -> Editor:
    return Editor("bot", uuid.uuid4(), name, uuid.uuid4())


async def test_a_lead_and_its_helper_are_one_team_with_one_drive(
    uow_factory: Any, org: Any
) -> None:
    manager = BotManager(uow_factory, None)  # type: ignore[arg-type]  # starts no runs here
    lead = await manager.create(org, name="Lead")
    other = await manager.create(org, name="Other")
    helper, _ = await BotService(uow_factory).create_helper(
        lead, run_id=uuid.uuid4(), step=0, name="Scout", label="", role="Find vendors"
    )
    assert lead.team_id == lead.id
    assert helper.team_id == lead.team_id
    assert other.team_id != lead.team_id

    drive = TeamDrive(uow_factory)
    await drive.write(team_of(lead), "/research/vendors.csv", "name,price\n", editor=PERSON)
    found = await drive.find(team_of(helper), "/RESEARCH/Vendors.CSV")
    assert found is not None and found.content == "name,price\n"
    assert await drive.find(team_of(other), "/research/vendors.csv") is None
    assert await drive.get(team_of(other), found.id) is None


async def test_every_change_is_a_revision_and_a_replay_does_not_repeat_it(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    team = Team(org, uuid.uuid4())
    scout = _bot_editor()
    op = uuid.uuid4()

    first = await drive.append(team, "/log.md", "found Acme", editor=scout, op_id=op)
    again = await drive.append(team, "/log.md", "found Acme", editor=scout, op_id=op)
    assert first.op == "create" and not first.replayed
    assert again.replayed and again.file.content == "found Acme" and again.file.version == 1

    await drive.append(team, "/log.md", "found Beta", editor=scout, op_id=uuid.uuid4())
    edited = await drive.edit(team, "/log.md", "Beta", "Bravo", editor=scout)
    assert edited.file.content == "found Acme\nfound Bravo" and edited.file.version == 3
    moved = await drive.move(team, "/log.md", "/archive/", editor=scout)
    assert moved.file.path == "/archive/log.md"

    history = await drive.revisions(team, moved.file.id)
    assert [r.op for r in history] == ["move", "edit", "append", "create"]
    assert history[0].note == "moved from /log.md"
    assert {r.editor_name for r in history} == {"Scout"}


async def test_a_path_is_one_file_whatever_its_case_and_a_deleted_one_can_come_back(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    team = Team(org, uuid.uuid4())
    made = await drive.create(team, "/Notes.md", "first", editor=PERSON)
    with pytest.raises(FileTakenError):
        await drive.create(team, "/notes.MD", "second", editor=PERSON)

    await drive.delete(team, "/notes.md", editor=_bot_editor())
    assert await drive.listing(team) == []
    assert [f.path for f in await drive.trash(team)] == ["/Notes.md"]

    # The path is free again; the deleted file comes back only once it is not taken.
    newer = await drive.create(team, "/notes.md", "newer", editor=PERSON)
    with pytest.raises(FileTakenError):
        await drive.restore(team, made.file.id, editor=PERSON)
    await drive.move(team, "/notes.md", "/notes-2.md", editor=PERSON)
    back = await drive.restore(team, made.file.id, editor=PERSON)
    assert back.file.deleted_at is None and back.file.content == "first"
    assert back.file.version == 3  # create, delete, restore
    assert {f.path for f in await drive.listing(team)} == {"/Notes.md", "/notes-2.md"}
    assert newer.file.id != made.file.id


async def test_a_restore_is_a_new_revision_and_history_is_pruned(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    team = Team(org, uuid.uuid4())
    made = await drive.create(team, "/draft.md", "v1", editor=PERSON)
    for i in range(2, 36):
        await drive.write(team, "/draft.md", f"v{i}", editor=PERSON)
    history = await drive.revisions(team, made.file.id)
    assert len(history) == 30 and history[0].version == 35 and history[-1].version == 6

    restored = await drive.restore(team, made.file.id, editor=PERSON, version=10)
    assert restored.file.content == "v10" and restored.file.version == 36


async def test_a_lock_binds_bots_not_the_person(uow_factory: Any, org: Any) -> None:
    drive = TeamDrive(uow_factory)
    team = Team(org, uuid.uuid4())
    made = await drive.create(team, "/rules.md", "Never pay more than $50.", editor=PERSON)
    await drive.set_locked(team, made.file.id, True)
    scout = _bot_editor()
    for attempt in (
        drive.append(team, "/rules.md", "x", editor=scout),
        drive.edit(team, "/rules.md", "$50", "$500", editor=scout),
        drive.move(team, "/rules.md", "/old.md", editor=scout),
        drive.delete(team, "/rules.md", editor=scout),
        drive.write(team, "/rules.md", "x", editor=scout, base_version=1),
    ):
        with pytest.raises(FileLockedError):
            await attempt
    changed = await drive.write(team, "/rules.md", "Never pay more than $40.", editor=PERSON)
    assert changed.file.version == 2


async def test_a_teams_files_outlive_its_lead_but_not_its_last_bot(
    uow_factory: Any, org: Any
) -> None:
    manager = BotManager(uow_factory, None)  # type: ignore[arg-type]
    lead = await manager.create(org, name="Lead")
    helper, _ = await BotService(uow_factory).create_helper(
        lead, run_id=uuid.uuid4(), step=0, name="Scout", label="", role="Find vendors"
    )
    drive = TeamDrive(uow_factory)
    await drive.create(team_of(lead), "/findings.md", "Acme is cheapest.", editor=PERSON)

    await manager.delete(lead.id, with_helpers=False)
    kept = await manager.get(helper.id)
    assert kept.parent_bot_id is None and kept.team_id == lead.team_id
    assert [f.path for f in await drive.listing(team_of(kept))] == ["/findings.md"]

    await manager.delete(helper.id, with_helpers=False)
    assert await drive.listing(team_of(kept)) == []
    assert [f.path for f in await drive.trash(team_of(kept))] == ["/findings.md"]


# --- bot_agent@1, against the real drive ---------------------------------------------------


class Interludes(ScriptedModel):
    """A scripted model that lets the world move between two of its steps — a teammate
    saving a file while this bot is mid-task."""

    def __init__(
        self, steps: list[dict[str, Any]], between: dict[int, Callable[[], Awaitable[Any]]]
    ) -> None:
        super().__init__(steps)
        self._between = between
        self._calls = 0

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        hook = self._between.get(self._calls)
        self._calls += 1
        if hook is not None:
            await hook()
        return await super().complete(ctx, req, work_class=work_class, call_site=call_site)


def _reply(text: str = "Done.") -> dict[str, Any]:
    return {"thought": "Report back", "action": "reply", "text": text}


def _node(bots: FakeBots, model: Any, drive: TeamDrive) -> _Node:
    return _Node(_Ctx(), FakePageGateway(), model, _Org(bots, files=drive))


def _activity(bots: FakeBots, action: str) -> list[Any]:
    return [m for m in bots.said("activity") if m.payload["action"]["type"] == action]


async def test_a_lead_writes_a_file_and_its_helper_reads_it(uow_factory: Any, org: Any) -> None:
    drive = TeamDrive(uow_factory)
    lead = _Bot(id=uuid.uuid4(), name="Lead", organization_id=org)
    lead_bots = FakeBots(lead)
    table = "name,price\nAcme,4\nBravo,6"
    await _turn(
        _node(
            lead_bots,
            ScriptedModel(
                [
                    {
                        "thought": "Save the table for Scout",
                        "action": "write_file",
                        "path": "research/vendors.csv",
                        "text": table,
                    },
                    _reply("Saved to /research/vendors.csv."),
                ]
            ),
            drive,
        ),
        lead.id,
    )
    (wrote,) = _activity(lead_bots, "write_file")
    assert wrote.payload["ok"] and wrote.payload["action"]["path"] == "/research/vendors.csv"
    assert wrote.payload["version"] == 1 and wrote.payload["file_id"]

    scout = _Bot(
        id=uuid.uuid4(),
        name="Scout",
        parent_bot_id=lead.id,
        team_id=lead.team_id,
        organization_id=org,
    )
    model = ScriptedModel(
        [
            {"thought": "Read it", "action": "read_file", "path": "/Research/Vendors.csv"},
            _reply("Acme is cheapest."),
        ]
    )
    await _turn(_node(FakeBots(scout), model, drive), scout.id)
    assert "/research/vendors.csv (" in model.system[0]
    assert "changed by Lead" in model.system[0]
    assert "<<<FILE" in model.prompts[1] and table in model.prompts[1]


async def test_a_bot_must_read_a_file_before_replacing_it(uow_factory: Any, org: Any) -> None:
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    await drive.create(team_of(bot), "/plan.md", "the person's plan", editor=PERSON)
    bots = FakeBots(bot)
    write = {"thought": "Rewrite", "action": "write_file", "path": "/plan.md", "text": "mine"}
    read = {"thought": "Read", "action": "read_file", "path": "/plan.md"}
    await _turn(
        _node(bots, ScriptedModel([write, read, write, _reply()]), drive),
        bot.id,
    )
    refused, done = _activity(bots, "write_file")
    assert refused.payload["ok"] is False and "read_file it first" in refused.payload["error"]
    assert done.payload["ok"] and done.payload["version"] == 2
    found = await drive.find(team_of(bot), "/plan.md")
    assert found is not None and found.content == "mine"


async def test_a_teammates_change_after_the_read_is_not_overwritten(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    team = team_of(bot)
    await drive.create(team, "/plan.md", "v1", editor=PERSON)
    writer = Editor("bot", uuid.uuid4(), "Writer", uuid.uuid4())

    async def teammate_saves() -> None:
        await drive.write(team, "/plan.md", "Writer's v2", editor=writer)

    bots = FakeBots(bot)
    read = {"thought": "Read", "action": "read_file", "path": "/plan.md"}
    write = {"thought": "Replace", "action": "write_file", "path": "/plan.md", "text": "mine"}
    model = Interludes([read, write, read, write, _reply()], between={1: teammate_saves})
    await _turn(_node(bots, model, drive), bot.id)

    stale, done = _activity(bots, "write_file")
    assert stale.payload["ok"] is False
    assert "changed after you read it" in stale.payload["error"]
    assert "changed by Writer" in stale.payload["error"]
    assert done.payload["ok"]
    history = await drive.revisions(team, (await drive.find(team, "/plan.md")).id)  # type: ignore[union-attr]
    assert [r.editor_name for r in history] == ["Scout", "Writer", "you"]


async def test_an_append_keeps_a_bot_current_only_if_nothing_landed_in_between(
    uow_factory: Any, org: Any
) -> None:
    """Read v1, append → v2: the bot has seen all of v2 and may replace it. Read v1, a
    teammate writes v2, append → v3: the bot never saw v2, so it must read again."""
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    team = team_of(bot)
    await drive.create(team, "/a.md", "a", editor=PERSON)
    await drive.create(team, "/b.md", "b", editor=PERSON)

    async def teammate_saves() -> None:
        await drive.write(team, "/b.md", "b, by Writer", editor=_bot_editor("Writer"))

    def steps(path: str) -> list[dict[str, Any]]:
        return [
            {"thought": "Read", "action": "read_file", "path": path},
            {"thought": "Add", "action": "append_file", "path": path, "text": "more"},
            {"thought": "Tidy", "action": "write_file", "path": path, "text": "tidied"},
        ]

    bots = FakeBots(bot)
    model = Interludes(steps("/a.md") + steps("/b.md") + [_reply()], between={4: teammate_saves})
    await _turn(_node(bots, model, drive), bot.id)
    a_write, b_write = _activity(bots, "write_file")
    assert a_write.payload["ok"]
    assert (
        b_write.payload["ok"] is False and "changed after you read it" in b_write.payload["error"]
    )


async def test_a_missing_file_names_the_one_it_probably_was(uow_factory: Any, org: Any) -> None:
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    await drive.create(team_of(bot), "/research/vendors.csv", "x", editor=PERSON)
    bots = FakeBots(bot)
    typo = {"thought": "Read", "action": "read_file", "path": "/reserch/vendors.csv"}
    await _turn(
        _node(bots, ScriptedModel([typo, _reply()]), drive),
        bot.id,
    )
    (miss,) = _activity(bots, "read_file")
    assert miss.payload["ok"] is False
    assert "Did you mean /research/vendors.csv?" in miss.payload["error"]


async def test_a_locked_file_is_refused_to_a_bot_in_words_it_can_act_on(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    made = await drive.create(team_of(bot), "/rules.md", "budget: $50", editor=PERSON)
    await drive.set_locked(team_of(bot), made.file.id, True)
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {"thought": "Raise it", "action": "edit_file", "path": "/rules.md"}
            | {"find": "$50", "text": "$500"},
            _reply(),
        ]
    )
    await _turn(_node(bots, model, drive), bot.id)
    (refused,) = _activity(bots, "edit_file")
    assert refused.payload["ok"] is False and "locked by your person" in refused.payload["error"]
    assert "edit_file /rules.md failed" in model.prompts[1]


async def test_listing_and_searching_put_the_drive_in_front_of_the_bot(
    uow_factory: Any, org: Any
) -> None:
    drive = TeamDrive(uow_factory)
    bot = _Bot(id=uuid.uuid4(), organization_id=org)
    team = team_of(bot)
    await drive.create(team, "/projects/acme/vendors.csv", "Acme,4", editor=PERSON)
    await drive.create(team, "/projects/beta/notes.md", "Beta launch", editor=PERSON)
    model = ScriptedModel(
        [
            {"thought": "Look", "action": "list_files", "path": "/projects/acme/"},
            {"thought": "Find", "action": "list_files", "text": "launch"},
            _reply(),
        ]
    )
    await _turn(_node(FakeBots(bot), model, drive), bot.id)
    assert "Files in /projects/acme (1):" in model.prompts[1]
    assert "/projects/beta" not in model.prompts[1].split("Results this turn")[1].split("PAGE")[0]
    assert "matching 'launch' (1)" in model.prompts[2]
    assert "/projects/beta/notes.md" in model.prompts[2]
    assert "YOUR TEAM DRIVE — 2 files" in model.system[0]


async def test_what_a_bot_read_rides_into_the_next_chunk_of_a_long_task() -> None:
    from runtime.domain.bots import MAX_STEPS
    from tests.test_bot_long_tasks import ChunkBots, _invoke

    bot = _Bot(id=uuid.uuid4(), turn=2)
    bots = ChunkBots(bot, max_chunks=3)
    state = {
        "input": {"bot_id": str(bot.id), "turn": 2},
        "n": MAX_STEPS,
        "files_read": {"/plan.md": 4},
    }
    await _invoke(_Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), state)
    (sent,) = bots.continued
    assert sent["carried"]["files_read"] == {"/plan.md": 4}


# --- the person's surface ------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    app.state.service = RunService(
        uow_factory, settings=settings, kill_switches=KillSwitchService(uow_factory)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http


async def test_the_person_browses_edits_and_restores_a_teams_files(
    api: httpx.AsyncClient,
) -> None:
    lead = (await api.post("/v1/bots", json={"name": "Lead"})).json()
    assert lead["team_id"] == lead["id"]
    files = f"/v1/bots/{lead['id']}/files"

    made = await api.post(files, json={"path": "notes/Todo.md", "content": "a\r\nb"})
    assert made.status_code == 201, made.text
    f = made.json()["file"]
    assert (f["path"], f["content"], f["version"], f["folder"]) == (
        "/notes/Todo.md",
        "a\nb",
        1,
        "/notes",
    )
    assert (await api.post(files, json={"path": "/NOTES/todo.md"})).status_code == 409

    listed = (await api.get(files, params={"q": "todo"})).json()
    assert [m["name"] for m in listed["team"]["members"]] == ["Lead"]
    assert [x["path"] for x in listed["files"]] == ["/notes/Todo.md"]
    assert "content" not in listed["files"][0]
    assert [x["path"] for x in listed["matches"]] == ["/notes/Todo.md"]

    one = f"{files}/{f['id']}"
    saved = await api.patch(one, json={"content": "c", "base_version": 1})
    assert saved.json()["file"]["version"] == 2
    late = await api.patch(one, json={"content": "d", "base_version": 1})
    assert late.status_code == 409 and "changed since you opened it" in late.json()["detail"]
    assert (await api.patch(one, json={"content": "d", "path": "/x.md"})).status_code == 422

    moved = (await api.patch(one, json={"path": "/archive/"})).json()["file"]
    assert moved["path"] == "/archive/Todo.md" and moved["version"] == 3
    assert (await api.patch(one, json={"locked": True})).json()["file"]["locked"] is True

    history = (await api.get(f"{one}/revisions")).json()["revisions"]
    assert [r["op"] for r in history] == ["move", "write", "create"]
    back = (await api.post(f"{one}/restore", json={"version": 1})).json()["file"]
    assert back["content"] == "a\nb" and back["version"] == 4

    assert (await api.delete(one, params={"base_version": 3})).status_code == 409
    assert (await api.delete(one, params={"base_version": 4})).status_code == 200
    listed = (await api.get(files)).json()
    assert listed["files"] == [] and [x["path"] for x in listed["trash"]] == ["/archive/Todo.md"]
    assert (await api.post(f"{one}/restore", json={})).json()["file"]["deleted_at"] is None

    other = (await api.post("/v1/bots", json={"name": "Other"})).json()
    assert (await api.get(f"/v1/bots/{other['id']}/files")).json()["files"] == []
    assert (await api.get(f"/v1/bots/{other['id']}/files/{f['id']}")).status_code == 404


def _record(**fields: Any) -> Any:
    """A file as rendering sees it (`domain.files.FileLike`), without a database."""
    base: dict[str, Any] = {
        "chars": 10,
        "locked": False,
        "updated_by_bot_id": None,
        "updated_by_name": "",
    }
    return SimpleNamespace(**(base | fields))
