"""Bots, as the graph and the API see them.

Two things live here.

**`bot_actor_spec`** — what a bot *is* to the runtime: an `LLM_AGENT` running
`bot_agent@1`, allowed the two browser tools, the terminal's three and connector calls,
and nothing else, on DeepSeek. The spec is
the same for every bot; what differs between them (name, instructions, memory) is
conversation state read at run time, not authority. So editing a bot's instructions
never republishes its actor, and "what was this run allowed to do" has the same answer
for every bot.

**`BotService`** — the conversation, through the narrow set of operations a run needs:
read the recent conversation, append activity with a deterministic id, park an action
for approval, check whether the person pressed Stop — and a bot's memory and brief:
what comes to mind for this conversation, remembering, forgetting, searching, the
diary line at the end of a turn, and revising a brief (its own, or a helper's). A node
reaches it through `node.org.bots`, the same route every M1 service takes.

Nothing here calls a model or the browser. Those go through the gateways.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from runtime.domain.bot_memory import (
    MAX_MEMORIES,
    BotBrief,
    BriefPatch,
    Memory,
    Recollection,
    brief_changes,
    clean_memory,
    find_duplicate,
    memory_id,
    revision_id,
    search,
    select_for_prompt,
    to_forget,
)
from runtime.domain.bots import (
    BOT_DELEGATION,
    MAX_HELPER_DEPTH,
    MAX_HELPERS,
    MAX_HISTORY_MESSAGES,
    MAX_STEPS,
    BotRule,
    actor_name_for,
    helper_id,
    message_id,
    pending_id,
)
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import ActorId, CorrelationId, OrganizationId
from runtime.domain.members import computer_profile
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles
from runtime.domain.vault import CredentialField, VaultOption, site_of
from runtime.domain.vault import request_id as credential_request_id
from runtime.org.department import FLASH, PRO
from runtime.org.inbox import KIND_BOT_CONTINUE, InboxService, dedupe_key
from runtime.persistence.repositories.bots import (
    BotMemoryRow,
    BotMessageRow,
    BotPendingRow,
    BotRow,
)
from runtime.persistence.repositories.vault import CredentialRequestRow, VaultEntryRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

BOT_GRAPH = "bot_agent@1"
BROWSER_TOOLS = frozenset({"browser.observe@1", "browser.act@1"})
TERMINAL_TOOLS = frozenset({"terminal.run@1", "workspace.read@1", "workspace.write@1"})
"""The shell and the shared workspace on the computer (`gateway.builtin.terminal`)."""
CONNECTOR_TOOLS = frozenset({"connector.call@1"})
"""Tools on MCP servers the organization connected (`gateway.builtin.connectors`)."""

_STEP_PROFILE = ModelProfile(
    provider="deepseek",
    model=PRO,
    max_output_tokens=2_000,
    temperature=0.0,
    input_cents_per_mtok=66,
    output_cents_per_mtok=198,
    # A step is a short structured decision made many times a turn. Thinking would
    # multiply the latency a person watching the screen feels on every click.
    thinking=False,
    effort="high",
)

_SUMMARY_PROFILE = ModelProfile(
    provider="deepseek",
    model=FLASH,
    max_output_tokens=2_000,
    temperature=0.0,
    input_cents_per_mtok=22,
    output_cents_per_mtok=66,
    thinking=False,
    effort="low",
)


VISION_MODEL = "deepseek-flash"
"""The DeepSeek model that takes images. Must stay in `gateway.providers.VISION_MODELS`."""

_LOOK_PROFILE = ModelProfile(
    provider="deepseek",
    model=VISION_MODEL,
    max_output_tokens=1_500,
    temperature=0.0,
    # Peak-hour list prices, so the ledger never under-counts: $0.30 in, $1.20 out per
    # million tokens. An image is at most 1,024 tokens — a look is a few hundredths of
    # a cent.
    input_cents_per_mtok=30,
    output_cents_per_mtok=120,
    # One question about one screenshot while a person watches the bot work: answered
    # straight off, like the step itself.
    thinking=False,
    effort="low",
)
"""Bots' vision (`look`). `deepseek-flash` is the DeepSeek model that reads images —
`deepseek-v4-pro`, which makes the bot's decisions, does not — so a bot's screenshots go
to the same vendor, through the same key, as everything else it does."""


def bot_actor_spec(actor_name: str) -> ActorSpec:
    return ActorSpec(
        name=actor_name,
        kind=ActorKind.LLM_AGENT,
        graph_ref=BOT_GRAPH,
        allowed_tools=BROWSER_TOOLS | TERMINAL_TOOLS | CONNECTOR_TOOLS,
        ceilings=Ceilings(
            # Derived from MAX_STEPS, because the step budget is what a turn is meant
            # to stop on: it ends with a progress report and carries on. A ceiling
            # below it ends the turn as a failure instead. Every pass looks before it
            # acts — two tool calls, observe and act — and makes one model call that
            # may need one corrective retry; the slack covers a resumed turn's parked
            # action and a fill. (It was 40 tool calls, which a busy turn hit at about
            # step 20.)
            # Three a step at most: the decision, a corrective retry, and Auto Review's
            # check of a risky step (`domain.review`) when the person has it on.
            max_llm_calls=3 * MAX_STEPS + 12,
            max_tool_calls=2 * MAX_STEPS + 12,
            max_wall_clock_s=1_800.0,
            max_cost_cents=300,
        ),
        model_profiles=ModelProfiles(
            profiles={
                WorkClass.WORK: _STEP_PROFILE,
                WorkClass.SUMMARIZATION: _SUMMARY_PROFILE,
                WorkClass.PERCEPTION: _LOOK_PROFILE,
            }
        ),
    )


KEEP_SCREENSHOTS = 100
"""Screenshots kept per bot for the chat. At tens of kilobytes each, a few megabytes a
bot; a message whose picture has aged out says so rather than breaking."""


class HelperRefusedError(Exception):
    """A helper the limits do not allow. The message is shown to the bot."""


class BriefLockedError(Exception):
    """The person locked this brief; no bot may change it. Shown to the bot."""


def as_memory(row: BotMemoryRow) -> Memory:
    return Memory(
        id=row.id,
        kind=row.kind,
        content=row.content,
        importance=row.importance,
        pinned=row.pinned,
        created_at=row.created_at,
        last_recalled_at=row.last_recalled_at,
        recall_count=row.recall_count,
        source_kind=row.source_kind,
        source_name=row.source_name,
    )


def brief_of(bot: Any) -> BotBrief:
    """A row's brief, tolerant of anything a person or an old version stored."""
    try:
        return BotBrief.model_validate(getattr(bot, "brief", None) or {})
    except ValueError:
        return BotBrief(notes=str(getattr(bot, "brief", "")))


@dataclass(frozen=True, slots=True)
class Remembered:
    memory_id: uuid.UUID
    outcome: str
    """`saved`, `merged` (it already knew this — the old memory got stronger),
    `revised`, or `unknown` (a revise naming a memory it does not have)."""


@dataclass(frozen=True, slots=True)
class BriefChange:
    brief: BotBrief
    rev: int | None
    changed: list[str]


async def write_brief(
    uow: UnitOfWork,
    bot_id: uuid.UUID,
    brief: BotBrief,
    *,
    revision: uuid.UUID,
    editor_kind: str,
    editor_bot_id: uuid.UUID | None = None,
    editor_name: str = "",
    reason: str = "",
    changed: list[str] | None = None,
) -> int | None:
    """Every brief write goes through here, so every one leaves a revision."""
    return await uow.bots.set_brief(
        bot_id,
        brief.model_dump(),
        revision_id=revision,
        editor_kind=editor_kind,
        editor_bot_id=editor_bot_id,
        editor_name=editor_name,
        reason=reason,
        changed=changed if changed is not None else brief_changes(BotBrief(), brief),
    )


async def store_memory(
    uow: UnitOfWork,
    bot_id: uuid.UUID,
    *,
    new_id: uuid.UUID,
    content: str,
    kind: str,
    importance: int,
    source_kind: str,
    source_name: str = "",
    pinned: bool = False,
) -> Remembered:
    """Save a memory: consolidate with a near-duplicate, then forget past the cap."""
    content = clean_memory(content)
    existing = [as_memory(r) for r in await uow.bots.memories(bot_id)]
    if any(m.id == new_id for m in existing):
        return Remembered(new_id, "saved")
    same = find_duplicate(existing, kind, content)
    if same is not None:
        await uow.bots.reinforce_memory(bot_id, same.id, importance)
        if len(content) > len(same.content):
            await uow.bots.update_memory(bot_id, same.id, {"content": content})
        return Remembered(same.id, "merged")
    await uow.bots.add_memory(
        new_id,
        bot_id,
        kind=kind,
        content=content,
        importance=importance,
        pinned=pinned,
        source_kind=source_kind,
        source_name=source_name,
    )
    doomed = to_forget(existing, dt.datetime.now(dt.UTC), cap=MAX_MEMORIES - 1)
    await uow.bots.delete_memories(bot_id, [m.id for m in doomed])
    return Remembered(new_id, "saved")


async def publish_bot_actor(
    uow: UnitOfWork, organization_id: OrganizationId, actor_name: str
) -> None:
    """Create a bot's actor, in the caller's transaction, at version 1.

    `Registrar.publish_actor` does the same for a person's bot; this is the copy a run
    can reach, because a helper is created from inside one and `runtime.org` may not
    import the runtime layer. It stays narrow on purpose: **the spec is always
    `bot_actor_spec` and the delegation limits always `BOT_DELEGATION`**, so a run that
    creates a helper chooses its name and nothing about what it may do. That is why
    this is not the config-plane write ARCHITECTURE.md keeps out of the worker — no
    run can use it to widen anything.
    """
    spec = bot_actor_spec(actor_name)
    actor_id = ActorId(uuid.uuid5(uuid.NAMESPACE_URL, f"botactor:{organization_id}:{actor_name}"))
    await uow.actors.create_actor(actor_id, organization_id, actor_name, spec.kind.value)
    version_id = await uow.actors.add_version(
        actor_id, 1, spec.model_dump(mode="json"), canonical_hash(spec)
    )
    await uow.actors.set_active_version(actor_id, version_id)
    await uow.actors.set_delegation(organization_id, actor_name, dict(BOT_DELEGATION))


async def refresh_bot_actor(
    uow: UnitOfWork, organization_id: OrganizationId, actor_name: str
) -> int | None:
    """Bring a bot's actor up to today's `bot_actor_spec`. Returns the new version, or
    `None` when it was already current.

    Every bot runs the same spec, so when the spec changes — a ceiling raised, a tool
    added — every bot published before the change is running the old one, and keeps
    running it: the spec is frozen into each actor version. This publishes the next
    version for a stale actor, in the caller's transaction. It is the same narrow
    write as `publish_bot_actor`: the spec is always `bot_actor_spec`, so it can only
    move a bot to what a new bot gets, never widen one.

    In-flight runs are unaffected — a run's spec was frozen at admission.
    """
    spec = bot_actor_spec(actor_name)
    wanted = canonical_hash(spec)
    current = await uow.actors.resolve_active(organization_id, actor_name)
    if current.spec_hash == wanted:
        return None
    version = current.version + 1
    version_id = await uow.actors.add_version(
        current.actor_id, version, spec.model_dump(mode="json"), wanted
    )
    await uow.actors.set_active_version(current.actor_id, version_id)
    return version


async def _audience(uow: UnitOfWork, bot: Any) -> uuid.UUID | None:
    """Whose devices hear about a bot: whoever last wrote to it (a team bot's
    conversation is shared), else its owner; None — every device — without members."""
    speaker = await uow.bots.last_speaker(bot.id)
    return speaker or getattr(bot, "owner_member_id", None)


class BotService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        inbox: InboxService | None = None,
        max_chunks: int = 1,
    ) -> None:
        self._uow = uow_factory
        self._inbox = inbox
        self._max_chunks = max_chunks if inbox is not None else 1

    @property
    def max_chunks(self) -> int:
        """Runs one instruction may take (`Settings.bot_auto_continue_chunks`); 1 when
        nothing would start the next one."""
        return self._max_chunks

    async def continue_later(
        self,
        bot: BotRow,
        *,
        run_id: Any,
        step: int,
        turn: int,
        chunk: int,
        carried: dict[str, Any],
    ) -> bool:
        """Carry a long task on in a fresh run. False when this was the last chunk.

        The next run is started by the dispatcher from a `bot.continue` message the
        bot sends its own actor — the same road a task assignment takes, so it is
        admitted, budgeted and kill-switched like any run, and it is deduped by the run
        that sent it (a replayed step sends nothing new). It carries the same `turn`:
        a new message from the person, or Stop, still ends the task at the next step.
        `carried` is the step log and working memory, which a new run would not have.
        """
        if self._inbox is None or chunk >= self._max_chunks:
            return False
        async with self._uow.transaction() as uow:
            await uow.bots.add_message(
                message_id(run_id, step, "continuing"),
                bot.id,
                role="system",
                content=(
                    f"Still working — this is a long task, so I'm carrying on "
                    f"(part {chunk + 1} of up to {self._max_chunks})."
                ),
                payload={"chunk": chunk + 1, "max_chunks": self._max_chunks},
                run_id=uuid.UUID(str(run_id)),
            )
            await self._inbox.send(
                organization_id=bot.organization_id,
                kind=KIND_BOT_CONTINUE,
                recipient=bot.actor_name,
                correlation_id=CorrelationId(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"botturn:{bot.id}:{turn}")
                ),
                key=dedupe_key("bot.continue", run_id),
                subject=f"{bot.name}: part {chunk + 1}",
                body={"bot_id": str(bot.id), "turn": turn, "chunk": chunk + 1, "carried": carried},
                sender=bot.actor_name,
                uow=uow,
            )
        return True

    async def save_screenshot(
        self,
        bot_id: uuid.UUID,
        *,
        run_id: Any,
        step: int,
        kind: str,
        data: bytes,
        page_url: str = "",
    ) -> uuid.UUID:
        """Keep a picture of the bot's screen for the chat. Returns its id, which a
        message carries as `screenshot_id`.

        Idempotent per `(run, step, kind)`, and bounded: past `KEEP_SCREENSHOTS` per bot
        the oldest go. The image is the computer's masked `bot_screenshot` — the same
        one a vision model would see — never the person's unmasked view.
        """
        sid = uuid.uuid5(uuid.NAMESPACE_URL, f"botshot:{run_id}:{step}:{kind}")
        async with self._uow.transaction() as uow:
            await uow.screenshots.add(
                sid,
                bot_id,
                run_id=uuid.UUID(str(run_id)),
                kind=kind,
                page_url=page_url,
                data=data,
            )
            await uow.screenshots.prune(bot_id, keep=KEEP_SCREENSHOTS)
        return sid

    async def claim_run(self, bot_id: uuid.UUID, run_id: Any) -> None:
        """Make this run the bot's current one. A chunk the dispatcher started is not
        one `BotManager` started, and the chat's "working" reads `last_run_id`."""
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(bot_id, last_run_id=uuid.UUID(str(run_id)))

    async def get(self, bot_id: uuid.UUID) -> BotRow | None:
        """The bot as its turn sees it: with Auto Review on when its organization
        requires it, whatever the bot's own switch says (`domain.policies`)."""
        async with self._uow() as uow:
            bot = await uow.bots.get(bot_id)
            required = (
                bot is not None
                and not bot.auto_review
                and (await uow.policies.get(bot.organization_id)).require_review
            )
        if required:
            assert bot is not None
            bot = dataclasses.replace(bot, auto_review=True)
        return bot

    async def by_actor(self, organization_id: OrganizationId, actor_name: str) -> BotRow | None:
        async with self._uow() as uow:
            return await uow.bots.get_by_actor(organization_id, actor_name)

    async def conversation(self, bot_id: uuid.UUID) -> list[BotMessageRow]:
        async with self._uow() as uow:
            return await uow.bots.recent_conversation(bot_id, MAX_HISTORY_MESSAGES)

    async def rules(self, bot_id: uuid.UUID) -> tuple[BotRule, ...]:
        async with self._uow() as uow:
            rows = await uow.bots.rules(bot_id)
        return tuple(
            BotRule(action_type=r.action_type, host=r.host, decision=r.decision)  # type: ignore[arg-type]
            for r in rows
        )

    async def stop_requested(self, bot_id: uuid.UUID) -> bool:
        bot = await self.get(bot_id)
        return bot is None or bot.stop_requested

    async def record(
        self,
        bot_id: uuid.UUID,
        *,
        run_id: Any,
        step: int,
        kind: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Append a run's message. Idempotent per `(run, step, kind)`."""
        mid = message_id(run_id, step, kind)
        async with self._uow.transaction() as uow:
            await uow.bots.add_message(
                mid,
                bot_id,
                role=role,
                content=content,
                payload=payload,
                run_id=uuid.UUID(str(run_id)),
            )
        return mid

    # --- memory ------------------------------------------------------------------

    async def memories(self, bot_id: uuid.UUID) -> list[Memory]:
        async with self._uow() as uow:
            return [as_memory(r) for r in await uow.bots.memories(bot_id)]

    async def recollect(self, bot_id: uuid.UUID, context: str, *, rehearse: bool) -> Recollection:
        """What comes to mind for this conversation. `rehearse` refreshes what was
        recalled — once a turn, not on every step of it."""
        memories = await self.memories(bot_id)
        recollection = select_for_prompt(memories, context, dt.datetime.now(dt.UTC))
        if rehearse and recollection.shown:
            async with self._uow.transaction() as uow:
                await uow.bots.mark_recalled(bot_id, [m.id for m in recollection.shown])
        return recollection

    async def resolve(self, bot_id: uuid.UUID, handle: str) -> Memory | None:
        """A memory by its short [id]."""
        handle = handle.strip().strip("[]").lower()
        if len(handle) < 4:
            return None
        matches = [m for m in await self.memories(bot_id) if m.id.hex.startswith(handle)]
        return matches[0] if len(matches) == 1 else None

    async def remember(
        self,
        bot_id: uuid.UUID,
        content: str,
        *,
        new_id: uuid.UUID,
        kind: str = "fact",
        importance: int = 3,
        source_kind: str = "self",
        source_name: str = "",
        revise: str | None = None,
    ) -> Remembered:
        if revise:
            target = await self.resolve(bot_id, revise)
            if target is None:
                return Remembered(new_id, "unknown")
            async with self._uow.transaction() as uow:
                await uow.bots.update_memory(
                    bot_id,
                    target.id,
                    {"content": clean_memory(content), "kind": kind, "importance": importance},
                )
            return Remembered(target.id, "revised")
        async with self._uow.transaction() as uow:
            return await store_memory(
                uow,
                bot_id,
                new_id=new_id,
                content=content,
                kind=kind,
                importance=importance,
                source_kind=source_kind,
                source_name=source_name,
            )

    async def forget(self, bot_id: uuid.UUID, handle: str) -> Memory | None:
        target = await self.resolve(bot_id, handle)
        if target is None:
            return None
        async with self._uow.transaction() as uow:
            await uow.bots.delete_memories(bot_id, [target.id])
        return target

    async def recall(
        self, bot_id: uuid.UUID, query: str
    ) -> tuple[list[Memory], list[BotMessageRow]]:
        """Search everything: memories ranked as recall ranks them, and the bot's own
        past conversation for the words themselves."""
        found = search(await self.memories(bot_id), query, dt.datetime.now(dt.UTC))
        async with self._uow.transaction() as uow:
            await uow.bots.mark_recalled(bot_id, [m.id for m in found])
            said = await uow.bots.search_bot_messages(bot_id, query)
        return found, said

    async def write_episode(
        self, bot_id: uuid.UUID, run_id: Any, content: str, *, importance: int = 2
    ) -> None:
        """The diary line at the end of a turn. One per run, whatever replays."""
        if not content.strip():
            return
        async with self._uow.transaction() as uow:
            await store_memory(
                uow,
                bot_id,
                new_id=memory_id("episode", run_id),
                content=content,
                kind="episode",
                importance=importance,
                source_kind="self",
            )

    # --- the brief -----------------------------------------------------------------

    async def update_brief(
        self,
        target: BotRow,
        patch: BriefPatch,
        *,
        revision: uuid.UUID,
        editor_kind: str,
        editor: BotRow,
        reason: str,
    ) -> BriefChange:
        """A bot revises a brief — its own (`self`) or a helper's (`parent`).

        Refused when the person has locked it. Idempotent per `revision`."""
        if target.brief_locked:
            whose = "your" if editor_kind == "self" else f"{target.name}'s"
            raise BriefLockedError(f"{whose} brief is locked by the person; ask them to change it")
        before = brief_of(target)
        after = patch.apply(before)
        changed = brief_changes(before, after)
        if not changed:
            return BriefChange(after, None, [])
        async with self._uow.transaction() as uow:
            rev = await write_brief(
                uow,
                target.id,
                after,
                revision=revision,
                editor_kind=editor_kind,
                editor_bot_id=editor.id,
                editor_name=editor.name,
                reason=reason,
                changed=changed,
            )
        return BriefChange(after, rev, changed)

    async def park(
        self,
        bot_id: uuid.UUID,
        *,
        run_id: Any,
        step: int,
        action: dict[str, Any],
        display: dict[str, Any],
        reason: str,
        thought: str,
        screenshot_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Park an action for a person to decide, and say so in the conversation.

        `action` is what will be executed and stays in `bot_pending_actions`;
        `display` is what the card shows, with any secret masked; `screenshot_id` is
        the screen the action would happen on, so the person decides on the page and
        not on the bot's account of it."""
        pid = pending_id(run_id, step)
        async with self._uow.transaction() as uow:
            await uow.bots.add_pending(
                pid, bot_id, run_id=uuid.UUID(str(run_id)), action=action, reason=reason
            )
            await uow.bots.add_message(
                message_id(run_id, step, "approval"),
                bot_id,
                role="approval",
                content=thought,
                payload={
                    "pending_id": str(pid),
                    "action": display,
                    "reason": reason,
                    **({"screenshot_id": str(screenshot_id)} if screenshot_id else {}),
                },
                run_id=uuid.UUID(str(run_id)),
            )
            await uow.bots.set_flags(bot_id, needs_attention=True, unread=True)
            bot = await uow.bots.get(bot_id)
            if bot is not None:
                await uow.push.notify(
                    message_id(run_id, step, "notify:approval"),
                    bot.organization_id,
                    bot_id=bot_id,
                    kind="approval",
                    title=f"{bot.name} needs your approval",
                    body=" ".join(thought.split())[:240],
                    url=f"/?bot={bot_id}",
                    member_id=await _audience(uow, bot),
                )
        return pid

    async def depth(self, bot_id: uuid.UUID) -> int:
        async with self._uow() as uow:
            return await uow.bots.depth(bot_id)

    async def helpers(self, bot_id: uuid.UUID) -> list[BotRow]:
        async with self._uow() as uow:
            return await uow.bots.children(bot_id)

    async def create_helper(
        self,
        parent: BotRow,
        *,
        run_id: Any,
        step: int,
        name: str,
        label: str,
        role: str,
        brief: BotBrief | None = None,
        seed_memories: list[str] | None = None,
    ) -> tuple[BotRow, bool]:
        """Create a helper under `parent`. Returns `(helper, created)`.

        Idempotent per `(run, step)`: a replayed step gets back the helper it already
        made. Refuses past `MAX_HELPERS` live helpers or `MAX_HELPER_DEPTH`, and
        refuses a second helper with the same name — `ask_bot` addresses helpers by
        name, so two of them would make "ask Scout" ambiguous.
        """
        hid = helper_id(run_id, step)
        async with self._uow.transaction() as uow:
            existing = await uow.bots.get(hid)
            if existing is not None:
                return existing, False
            depth = await uow.bots.depth(parent.id)
            if depth + 1 > MAX_HELPER_DEPTH:
                raise HelperRefusedError(
                    f"you are already {depth} level(s) below a person's bot; helpers can "
                    f"only go {MAX_HELPER_DEPTH} levels deep. Do the work yourself or ask "
                    "an existing helper."
                )
            siblings = await uow.bots.children(parent.id)
            if len(siblings) >= MAX_HELPERS:
                names = ", ".join(b.name for b in siblings)
                raise HelperRefusedError(
                    f"you already have {MAX_HELPERS} helpers ({names}); reuse one of them"
                )
            if any(b.name.lower() == name.lower() for b in siblings):
                raise HelperRefusedError(
                    f"you already have a helper called {name!r}; ask it, or pick another name"
                )
            actor_name = actor_name_for(name, hid.hex[:6])
            await publish_bot_actor(uow, parent.organization_id, actor_name)
            await uow.bots.create(
                hid,
                parent.organization_id,
                actor_name=actor_name,
                name=name,
                label=label,
                description=role,
                avatar="",
                parent_bot_id=parent.id,
                created_by="bot",
            )
            first = brief or BotBrief()
            if not first.mission.strip():
                first = first.model_copy(update={"mission": role})
            await write_brief(
                uow,
                hid,
                first,
                revision=revision_id(run_id, step, hid),
                editor_kind="parent",
                editor_bot_id=parent.id,
                editor_name=parent.name,
                reason=f"Created by {parent.name}",
            )
            for i, fact in enumerate(seed_memories or []):
                if fact.strip():
                    await store_memory(
                        uow,
                        hid,
                        new_id=memory_id("seed", hid, i),
                        content=fact,
                        kind="fact",
                        importance=3,
                        source_kind="parent",
                        source_name=parent.name,
                    )
            created = await uow.bots.get(hid)
        assert created is not None
        return created, True

    async def pending(self, pending_id_: uuid.UUID) -> BotPendingRow | None:
        async with self._uow() as uow:
            return await uow.bots.get_pending(pending_id_)

    async def notify(
        self, bot: Any, kind: str, body: str, *, run_id: Any, step: int, url: str = ""
    ) -> None:
        """Tell the person's devices (`runtime.runtime.notifier` sends it). Idempotent
        per `(run, step)`, like the line in the conversation it is about."""
        title = {
            "question": f"{bot.name} has a question",
            "approval": f"{bot.name} needs your approval",
            "sign_in": f"{bot.name} needs you to sign in",
        }.get(kind, bot.name)
        async with self._uow.transaction() as uow:
            await uow.push.notify(
                message_id(run_id, step, f"notify:{kind}"),
                bot.organization_id,
                bot_id=bot.id,
                kind=kind,
                title=title,
                body=" ".join(body.split())[:240],
                url=url or f"/?bot={bot.id}",
                member_id=await _audience(uow, bot),
            )

    async def end_turn(self, bot_id: uuid.UUID, *, needs_attention: bool = False) -> None:
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(
                bot_id, unread=True, needs_attention=needs_attention, stop_requested=False
            )
            await uow.bots.touch(bot_id)

    # --- the login vault -----------------------------------------------------------
    #
    # Metadata only. Nothing here can decrypt an entry — that is `runtime.gateway.vault`,
    # opened by the browser tool at the moment it types — so a run deciding whether to
    # fill a form knows which logins exist and what they can answer, and nothing more.

    async def vault_options(self, bot: BotRow, host: str) -> list[VaultOption]:
        async with self._uow() as uow:
            rows = await uow.vault.options(
                bot.organization_id, site_of(host), bot.id, computer_profile(bot)
            )
        return [vault_option(r) for r in rows]

    async def request_credentials(
        self,
        bot: BotRow,
        *,
        run_id: Any,
        step: int,
        host: str,
        page_url: str,
        purpose: str,
        fields: list[CredentialField],
        ask: set[str],
        thought: str,
        saved: list[VaultOption],
        retry: bool,
        reason: str,
        working: dict[str, Any] | None = None,
        screenshot_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Put a credential card in the conversation. Idempotent per `(run, step)`.

        The request keeps every field of the form (a confirm box is filled, not
        asked); the card shows the ones in `ask`. Neither holds a value — there is
        none yet, and when there is, it goes to the vault, not here.
        """
        rid = credential_request_id(run_id, step)
        shown = [f.as_dict() | {"ask": f.key in ask} for f in fields]
        async with self._uow.transaction() as uow:
            await uow.vault.add_request(
                rid,
                bot.id,
                run_id=uuid.UUID(str(run_id)),
                host=site_of(host),
                page_url=page_url,
                purpose=purpose,
                fields=shown,
                working=working,
            )
            await uow.bots.add_message(
                message_id(run_id, step, "credentials"),
                bot.id,
                role="credentials",
                content=thought,
                payload={
                    "credential_request_id": str(rid),
                    "host": site_of(host),
                    "page_url": page_url[:500],
                    "purpose": purpose,
                    "fields": [
                        {"key": f.key, "kind": f.kind, "label": f.label}
                        for f in fields
                        if f.key in ask
                    ],
                    "saved": [{"id": str(o.id), "label": o.label} for o in saved],
                    "retry": retry,
                    "reason": reason,
                    **({"screenshot_id": str(screenshot_id)} if screenshot_id else {}),
                },
                run_id=uuid.UUID(str(run_id)),
            )
            await uow.bots.set_flags(bot.id, needs_attention=True, unread=True)
            await uow.push.notify(
                message_id(run_id, step, "notify:sign_in"),
                bot.organization_id,
                bot_id=bot.id,
                kind="sign_in",
                title=f"{bot.name} needs you to sign in",
                body=f"{site_of(host)} — {purpose.replace('_', ' ')}",
                url=f"/?bot={bot.id}",
                member_id=await _audience(uow, bot),
            )
        return rid

    async def credential_request(self, request_id: uuid.UUID) -> CredentialRequestRow | None:
        async with self._uow() as uow:
            return await uow.vault.get_request(request_id)


def vault_option(row: VaultEntryRow) -> VaultOption:
    return VaultOption(
        id=row.id,
        kind="once" if row.kind == "once" else "saved",
        host=row.host,
        kinds=frozenset(row.kinds),
        label=row.label,
        auto_use=row.auto_use,
    )
