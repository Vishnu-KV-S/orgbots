"""Bots, as the graph and the API see them.

Two things live here.

**`bot_actor_spec`** — what a bot *is* to the runtime: an `LLM_AGENT` running
`bot_agent@1`, allowed the two browser tools and nothing else, on DeepSeek. The spec is
the same for every bot; what differs between them (name, instructions, memory) is
conversation state read at run time, not authority. So editing a bot's instructions
never republishes its actor, and "what was this run allowed to do" has the same answer
for every bot.

**`BotService`** — the conversation, through the narrow set of operations a run needs:
read the recent conversation, append activity with a deterministic id, save a note,
park an action for approval, and check whether the person pressed Stop. A node reaches
it through `node.org.bots`, the same route every M1 service takes.

Nothing here calls a model or the browser. Those go through the gateways.
"""

from __future__ import annotations

import uuid
from typing import Any

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
    trim_memory,
)
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import ActorId, OrganizationId
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles
from runtime.org.department import FLASH, PRO
from runtime.persistence.repositories.bots import BotMessageRow, BotPendingRow, BotRow
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

    async def remember(self, bot_id: uuid.UUID, note: str) -> str:
        async with self._uow.transaction() as uow:
            bot = await uow.bots.get(bot_id)
            if bot is None:
                return ""
            updated = trim_memory(bot.memory, note)
            await uow.bots.append_memory(bot_id, updated)
        return updated

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
                instructions=role,
                avatar="",
                parent_bot_id=parent.id,
                created_by="bot",
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
