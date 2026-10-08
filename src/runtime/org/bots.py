"""Bots, as the graph and the API see them.

Two things live here.

**`bot_actor_spec`** — what a bot *is* to the runtime: an `LLM_AGENT` running
`bot_agent@1`, allowed the two browser tools and nothing else, on DeepSeek. The spec is
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
    BotRule,
    actor_name_for,
    helper_id,
    message_id,
    pending_id,
)
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import ActorId, OrganizationId
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles
from runtime.org.department import FLASH, PRO
from runtime.persistence.repositories.bots import (
    BotMemoryRow,
    BotMessageRow,
    BotPendingRow,
    BotRow,
)
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

BOT_GRAPH = "bot_agent@1"
BROWSER_TOOLS = frozenset({"browser.observe@1", "browser.act@1"})

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


def bot_actor_spec(actor_name: str) -> ActorSpec:
    return ActorSpec(
        name=actor_name,
        kind=ActorKind.LLM_AGENT,
        graph_ref=BOT_GRAPH,
        allowed_tools=BROWSER_TOOLS,
        ceilings=Ceilings(
            # MAX_STEPS browser actions, each preceded by one model call, plus the
            # observation that opens the turn and room for one corrective retry per
            # step's schema.
            max_llm_calls=60,
            max_tool_calls=40,
            max_wall_clock_s=1_800.0,
            max_cost_cents=300,
        ),
        model_profiles=ModelProfiles(
            profiles={WorkClass.WORK: _STEP_PROFILE, WorkClass.SUMMARIZATION: _SUMMARY_PROFILE}
        ),
    )


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


class BotService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def get(self, bot_id: uuid.UUID) -> BotRow | None:
        async with self._uow() as uow:
            return await uow.bots.get(bot_id)

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
    ) -> uuid.UUID:
        """Park an action for a person to decide, and say so in the conversation.

        `action` is what will be executed and stays in `bot_pending_actions`;
        `display` is what the card shows, with any secret masked."""
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
                payload={"pending_id": str(pid), "action": display, "reason": reason},
                run_id=uuid.UUID(str(run_id)),
            )
            await uow.bots.set_flags(bot_id, needs_attention=True, unread=True)
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

    async def end_turn(self, bot_id: uuid.UUID, *, needs_attention: bool = False) -> None:
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(
                bot_id, unread=True, needs_attention=needs_attention, stop_requested=False
            )
            await uow.bots.touch(bot_id)
