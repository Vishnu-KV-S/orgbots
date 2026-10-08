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
    MAX_HISTORY_MESSAGES,
    BotRule,
    message_id,
    pending_id,
    trim_memory,
)
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles
from runtime.org.department import FLASH, PRO
from runtime.persistence.repositories.bots import BotMessageRow, BotPendingRow, BotRow
from runtime.persistence.uow import UnitOfWorkFactory

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

    async def pending(self, pending_id_: uuid.UUID) -> BotPendingRow | None:
        async with self._uow() as uow:
            return await uow.bots.get_pending(pending_id_)

    async def end_turn(self, bot_id: uuid.UUID, *, needs_attention: bool = False) -> None:
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(
                bot_id, unread=True, needs_attention=needs_attention, stop_requested=False
            )
            await uow.bots.touch(bot_id)
