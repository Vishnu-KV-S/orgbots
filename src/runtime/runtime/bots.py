"""Managing bots: creating them, talking to them, deciding on what they ask.

The one rule this module keeps is I1's: **a bot's run is created by
`RunService.start_run()` and nothing else.** A message to a bot is a run of its actor
with `{bot_id, turn}` as input, so it is admitted, budgeted, kill-switched and audited
exactly like a cron firing in the marketing department. A bot that has hit its budget
gets a refused run and a line in the conversation saying so, not a silent no-op.

Creating a bot publishes an actor (`bot_actor_spec`) and writes a `bots` row in the
same breath; the actor is the authority and the row is the face. Editing a bot's
brief changes the row, never the actor, so it is not a republish — what a bot *may
do* does not change when a person rewords what it *should do*. Every brief change a
person makes is a revision, like one a bot makes, so the history has no gaps.

Duplicating copies the brief and rules but not memory or conversation, the way
GrokBot documents it: a copy is a new employee with the same job description.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from runtime.domain.bot_memory import BotBrief, brief_changes, clean_memory
from runtime.domain.bots import actor_name_for, host_of
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import StartRunRequest
from runtime.observability.logging import get_logger
from runtime.org.bots import brief_of, publish_bot_actor, store_memory, write_brief
from runtime.persistence.repositories.bots import BotRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService

log = get_logger("runtime.bots")


class BotNotFoundError(LookupError):
    pass


class PendingNotFoundError(LookupError):
    pass


class MemoryNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class Sent:
    message_id: uuid.UUID
    run_id: uuid.UUID | None
    admitted: bool
    refusal_reason: str | None


class BotManager:
    def __init__(self, uow_factory: UnitOfWorkFactory, runs: RunService) -> None:
        self._uow = uow_factory
        self._runs = runs
        self._registrar = Registrar(uow_factory)

    async def ensure_organization(self, organization_id: OrganizationId, name: str) -> None:
        await self._registrar.ensure_organization(organization_id, name)

    # --- lifecycle -------------------------------------------------------------------

    async def create(
        self,
        organization_id: OrganizationId,
        *,
        name: str,
        label: str = "",
        description: str = "",
        avatar: str = "",
        brief: BotBrief | None = None,
        duplicated_from: uuid.UUID | None = None,
        appearance: dict[str, Any] | None = None,
    ) -> BotRow:
        bot_id = uuid.uuid4()
        actor_name = actor_name_for(name, secrets.token_hex(3))
        # Actor and row in one transaction: a bot row pointing at an actor that failed
        # to publish would be a bot whose every message is refused for a reason nobody
        # can see. `publish_bot_actor` is the same path a helper takes, so a person's bot
        # and a bot's helper are the same kind of actor with the same delegation limits.
        async with self._uow.transaction() as uow:
            await publish_bot_actor(uow, organization_id, actor_name)
            await uow.bots.create(
                bot_id,
                organization_id,
                actor_name=actor_name,
                name=name,
                label=label,
                description=description,
                avatar=avatar,
                duplicated_from=duplicated_from,
                appearance=appearance,
            )
            if brief is not None and not brief.is_empty():
                await write_brief(
                    uow,
                    bot_id,
                    brief,
                    revision=uuid.uuid4(),
                    editor_kind="person",
                    reason="Copied with the bot" if duplicated_from else "Written at creation",
                )
            row = await uow.bots.get(bot_id)
        assert row is not None
        log.info("bot.created", bot_id=str(bot_id), actor=actor_name)
        return row

    async def get(self, bot_id: uuid.UUID) -> BotRow:
        async with self._uow() as uow:
            row = await uow.bots.get(bot_id)
        if row is None:
            raise BotNotFoundError(str(bot_id))
        return row

    async def duplicate(self, bot_id: uuid.UUID) -> BotRow:
        source = await self.get(bot_id)
        copy = await self.create(
            source.organization_id,
            name=f"{source.name} (copy)",
            label=source.label,
            description=source.description,
            avatar=source.avatar,
            brief=brief_of(source),
            duplicated_from=source.id,
            appearance=source.appearance,
        )
        async with self._uow.transaction() as uow:
            await uow.bots.copy_rules(source.id, copy.id)
        return copy

    async def update(self, bot_id: uuid.UUID, fields: dict[str, Any]) -> BotRow:
        await self.get(bot_id)
        async with self._uow.transaction() as uow:
            await uow.bots.update(bot_id, fields)
        return await self.get(bot_id)

    # --- the brief -------------------------------------------------------------------

    async def set_brief(
        self, bot_id: uuid.UUID, brief: BotBrief, *, reason: str = "", locked: bool | None = None
    ) -> BotRow:
        """The person edits a brief. A person may edit a locked brief — the lock is
        theirs — and may lock or unlock it."""
        bot = await self.get(bot_id)
        before = brief_of(bot)
        changed = brief_changes(before, brief)
        async with self._uow.transaction() as uow:
            if changed:
                await write_brief(
                    uow,
                    bot_id,
                    brief,
                    revision=uuid.uuid4(),
                    editor_kind="person",
                    reason=reason,
                    changed=changed,
                )
                await uow.bots.add_message(
                    uuid.uuid4(),
                    bot_id,
                    role="system",
                    content=f"You updated {bot.name}'s brief ({', '.join(changed)})"
                    + (f": {reason}" if reason else "."),
                    payload={"brief_changed": changed},
                )
            if locked is not None:
                await uow.bots.update(bot_id, {"brief_locked": locked})
        return await self.get(bot_id)

    async def restore_brief(self, bot_id: uuid.UUID, rev: int) -> BotRow:
        async with self._uow() as uow:
            revision = await uow.bots.brief_revision(bot_id, rev)
        if revision is None:
            raise LookupError(f"no revision {rev}")
        return await self.set_brief(
            bot_id, BotBrief.model_validate(revision.brief), reason=f"Restored revision {rev}"
        )

    # --- memory ----------------------------------------------------------------------

    async def add_memory(
        self, bot_id: uuid.UUID, *, content: str, kind: str, importance: int, pinned: bool
    ) -> uuid.UUID:
        await self.get(bot_id)
        async with self._uow.transaction() as uow:
            saved = await store_memory(
                uow,
                bot_id,
                new_id=uuid.uuid4(),
                content=content,
                kind=kind,
                importance=importance,
                pinned=pinned,
                source_kind="person",
                source_name="you",
            )
            if pinned:
                await uow.bots.update_memory(bot_id, saved.memory_id, {"pinned": True})
        return saved.memory_id

    async def edit_memory(
        self, bot_id: uuid.UUID, memory_id: uuid.UUID, fields: dict[str, Any]
    ) -> None:
        if "content" in fields:
            fields["content"] = clean_memory(str(fields["content"]))
        async with self._uow.transaction() as uow:
            if not await uow.bots.update_memory(bot_id, memory_id, fields):
                raise MemoryNotFoundError(str(memory_id))

    async def delete_memory(self, bot_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        async with self._uow.transaction() as uow:
            if not await uow.bots.delete_memories(bot_id, [memory_id]):
                raise MemoryNotFoundError(str(memory_id))

    async def clear_memories(self, bot_id: uuid.UUID) -> int:
        async with self._uow.transaction() as uow:
            return await uow.bots.clear_memories(bot_id)

    async def delete(self, bot_id: uuid.UUID, *, with_helpers: bool) -> list[uuid.UUID]:
        """Delete a bot. Returns every bot id that was deleted.

        The person decides what happens to the helpers under it. `with_helpers` deletes
        the whole subtree; otherwise its direct helpers move up to its own parent (or
        to the top level) and keep working — nothing below a deleted bot is ever
        orphaned or deleted without being asked about.
        """
        bot = await self.get(bot_id)
        async with self._uow.transaction() as uow:
            doomed = [bot_id]
            if with_helpers:
                doomed += await uow.bots.descendants(bot_id)
            else:
                await uow.bots.reparent_children(bot_id, bot.parent_bot_id)
            for victim in doomed:
                await uow.bots.set_flags(victim, stop_requested=True)
                await uow.bots.soft_delete(victim)
        log.info("bot.deleted", bot_id=str(bot_id), with_helpers=with_helpers, count=len(doomed))
        return doomed

    async def mark_read(self, bot_id: uuid.UUID, unread: bool = False) -> None:
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(bot_id, unread=unread)

    async def stop(self, bot_id: uuid.UUID) -> None:
        await self.get(bot_id)
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(bot_id, stop_requested=True)

    # --- conversation ----------------------------------------------------------------

    async def send(
        self, bot_id: uuid.UUID, text: str, *, reply_to: uuid.UUID | None = None
    ) -> Sent:
        """A person's message: record it, supersede whatever was running, start a run."""
        bot = await self.get(bot_id)
        message_id = uuid.uuid4()
        async with self._uow.transaction() as uow:
            await uow.bots.add_message(
                message_id, bot_id, role="user", content=text, reply_to=reply_to
            )
            # A new instruction makes anything still waiting for approval moot; the
            # bot will re-propose it if it still applies.
            await uow.bots.expire_pending(bot_id)
            turn = await uow.bots.bump_turn(bot_id)
        return await self._start(
            bot, turn, key=f"bot:{bot_id}:msg:{message_id}", extra={}, anchor=message_id
        )

    async def decide(self, bot_id: uuid.UUID, pending_id: uuid.UUID, decision: str) -> Sent:
        """Allow once, always allow, or deny a parked action — then resume the bot.

        Deny resumes too: the bot is told the action was refused and has to find
        another way or ask, rather than sitting in a conversation that silently ended.
        """
        bot = await self.get(bot_id)
        if decision not in ("once", "always", "deny"):
            raise ValueError("decision must be once, always or deny")
        async with self._uow.transaction() as uow:
            pending = await uow.bots.get_pending(pending_id)
            if pending is None or pending.bot_id != bot_id:
                raise PendingNotFoundError(str(pending_id))
            status = "denied" if decision == "deny" else "allowed"
            won = await uow.bots.decide_pending(
                pending_id, status=status, scope=None if decision == "deny" else decision
            )
            if not won:
                raise PendingNotFoundError(f"{pending_id} was already decided")
            if decision == "always":
                action_type = str(pending.action.get("type", "*"))
                host = str(
                    pending.action.get("host") or host_of(str(pending.action.get("page_url", "")))
                )
                await uow.bots.put_rule(bot_id, action_type, host, "allow")
            label = {"once": "Allowed once", "always": "Always allowed", "deny": "Denied"}[decision]
            note_id = uuid.uuid4()
            await uow.bots.add_message(
                note_id,
                bot_id,
                role="system",
                content=label,
                payload={"pending_id": str(pending_id), "decision": decision},
            )
            turn = await uow.bots.bump_turn(bot_id)
        return await self._start(
            bot,
            turn,
            key=f"bot:{bot_id}:pending:{pending_id}",
            extra={"resume_pending_id": str(pending_id)},
            anchor=note_id,
        )

    async def _start(
        self,
        bot: BotRow,
        turn: int,
        *,
        key: str,
        extra: dict[str, Any],
        anchor: uuid.UUID,
    ) -> Sent:
        result = await self._runs.start_run(
            StartRunRequest(
                organization_id=bot.organization_id,
                actor_name=bot.actor_name,
                input={"bot_id": str(bot.id), "turn": turn, **extra},
                idempotency_key=key,
            )
        )
        async with self._uow.transaction() as uow:
            await uow.bots.set_flags(bot.id, last_run_id=result.run_id)
            if not result.admitted:
                await uow.bots.add_message(
                    uuid.uuid4(),
                    bot.id,
                    role="error",
                    content=(
                        "I couldn't start working on that: "
                        f"{result.refusal_reason or 'the run was refused'}."
                    ),
                    run_id=result.run_id,
                )
                await uow.bots.set_flags(bot.id, needs_attention=True, unread=True)
        return Sent(
            message_id=anchor,
            run_id=result.run_id,
            admitted=result.admitted,
            refusal_reason=result.refusal_reason,
        )
