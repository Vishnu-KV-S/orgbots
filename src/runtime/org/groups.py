"""Group chats and bot-to-bot messages, as the graph and the API see them.

A run reaches this through `node.org.groups`: read a group's conversation (or one of
its threads), post a bot's answer there, send another bot a message, relay a reply
back. Each of those that involves another bot also **queues a wake** for it — a row the
runner (`runtime.runtime.wakes`) turns into a turn when that bot is free — in the same
transaction as the message, so a message never sits in a conversation with nobody
coming for it, and a wake never points at a message that was not written.

The limits are checked here, where every path passes: at most `MAX_HOPS` deliveries
deep from the person's message, at most `DAILY_BOT_MESSAGES` from one bot a day, and
a reply never asks for a reply back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from runtime.domain.bots import message_id
from runtime.domain.groups import (
    DAILY_BOT_MESSAGES,
    GROUP_HISTORY,
    MAX_HOPS,
    MAX_MEMBERS,
    MIN_MEMBERS,
    Member,
    recipients,
    wake_id,
)
from runtime.persistence.repositories.groups import GroupMessageRow, GroupRow, WakeRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory


class GroupError(ValueError):
    """A group or a message that cannot be as asked. Shown to whoever asked."""


@dataclass(frozen=True, slots=True)
class Said:
    """A group message in the shape a bot's prompt reads conversation rows in."""

    role: str
    content: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Delivered:
    recipient_id: uuid.UUID
    recipient_name: str
    queued: bool


class GroupService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    # --- groups ----------------------------------------------------------------------

    async def create(
        self,
        organization_id: uuid.UUID,
        name: str,
        members: list[uuid.UUID],
        lead: uuid.UUID | None = None,
        *,
        owner_member_id: uuid.UUID | None = None,
    ) -> GroupRow:
        members = list(dict.fromkeys(members))
        async with self._uow.transaction() as uow:
            await self._check_members(uow, organization_id, members)
            gid = uuid.uuid4()
            await uow.groups.create(
                gid,
                organization_id,
                name.strip(),
                members,
                lead if lead in members else members[0],
                owner_member_id=owner_member_id,
            )
            row = await uow.groups.get(gid)
        assert row is not None
        return row

    async def update(
        self,
        group: GroupRow,
        *,
        name: str | None = None,
        members: list[uuid.UUID] | None = None,
        lead: uuid.UUID | None = None,
    ) -> GroupRow:
        async with self._uow.transaction() as uow:
            current = [m[0] for m in await uow.groups.members(group.id)]
            if members is not None:
                members = list(dict.fromkeys(members))
                await self._check_members(uow, group.organization_id, members)
                await uow.groups.set_members(group.id, members)
                current = members
            fields: dict[str, Any] = {}
            if name is not None and name.strip():
                fields["name"] = name.strip()
            wanted_lead = lead if lead is not None else group.lead_bot_id
            if wanted_lead not in current:
                wanted_lead = current[0] if current else None
            if wanted_lead != group.lead_bot_id:
                fields["lead_bot_id"] = wanted_lead
            await uow.groups.update(group.id, **fields)
            row = await uow.groups.get(group.id)
        assert row is not None
        return row

    @staticmethod
    async def _check_members(
        uow: UnitOfWork, organization_id: uuid.UUID, members: list[uuid.UUID]
    ) -> None:
        if not MIN_MEMBERS <= len(members) <= MAX_MEMBERS:
            raise GroupError(f"a group has {MIN_MEMBERS} to {MAX_MEMBERS} bots")
        live = {b.id for b in await uow.bots.list_for(organization_id)}  # type: ignore[arg-type]
        missing = [m for m in members if m not in live]
        if missing:
            raise GroupError(f"no bot {missing[0]} in this organization")

    async def get(self, group_id: uuid.UUID) -> GroupRow | None:
        async with self._uow() as uow:
            return await uow.groups.get(group_id)

    async def members(self, group_id: uuid.UUID) -> list[Member]:
        async with self._uow() as uow:
            return [Member(b, name) for b, name, _ in await uow.groups.members(group_id)]

    async def roster(self, group_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
        async with self._uow() as uow:
            return await uow.groups.members(group_id)

    async def conversation(
        self, group_id: uuid.UUID, *, thread_root: uuid.UUID | None = None
    ) -> list[Said]:
        """What a bot reads on a group turn: the recent main conversation, and the
        whole thread when the turn is in one."""
        async with self._uow() as uow:
            rows = await uow.groups.recent(group_id, GROUP_HISTORY)
            if thread_root is not None:
                rows = rows[-10:] + await uow.groups.recent(
                    group_id, GROUP_HISTORY, thread_root=thread_root
                )
        return [_said(r) for r in rows]

    # --- a bot's answer in a group -----------------------------------------------------

    async def post_reply(
        self,
        group_id: uuid.UUID,
        bot: Any,
        text: str,
        *,
        run_id: Any,
        step: int,
        hops: int,
        thread_root: uuid.UUID | None = None,
    ) -> list[Delivered]:
        """Post a bot's answer, and wake the teammates it named. Idempotent per step."""
        async with self._uow.transaction() as uow:
            group = await uow.groups.get(group_id)
            if group is None:
                return []
            mid = message_id(run_id, step, "group")
            await uow.groups.add_message(
                mid,
                group_id,
                author_kind="bot",
                author_bot_id=bot.id,
                author_name=bot.name,
                content=text,
                payload={"hops": hops},
                thread_root=thread_root,
                run_id=uuid.UUID(str(run_id)),
            )
            await uow.groups.touch(group_id, unread=True)
            members = [Member(b, n) for b, n, _ in await uow.groups.members(group_id)]
            named = recipients(text, members, lead=None, author=bot.id)
            return await self._wake_members(
                uow,
                group_id,
                named,
                members,
                from_bot=bot,
                cause=mid,
                hops=hops + 1,
                thread_root=thread_root,
            )

    async def _wake_members(
        self,
        uow: UnitOfWork,
        group_id: uuid.UUID,
        named: list[uuid.UUID],
        members: list[Member],
        *,
        from_bot: Any | None,
        cause: uuid.UUID,
        hops: int,
        thread_root: uuid.UUID | None,
    ) -> list[Delivered]:
        names = {m.bot_id: m.name for m in members}
        out: list[Delivered] = []
        if from_bot is not None and named:
            if hops > MAX_HOPS:
                await self._note(uow, group_id, "(Not passed on: too many bot-to-bot handoffs.)")
                return [Delivered(b, names[b], False) for b in named]
            if await uow.groups.sent_today(from_bot.id) + len(named) > DAILY_BOT_MESSAGES:
                await self._note(
                    uow,
                    group_id,
                    f"(Not passed on: {from_bot.name} reached its "
                    "daily limit of messages to other bots.)",
                )
                return [Delivered(b, names[b], False) for b in named]
        for bot_id in named:
            queued = await uow.groups.add_wake(
                wake_id("group", cause, bot_id),
                bot_id,
                kind="group",
                hops=hops,
                group_id=group_id,
                group_message_id=cause,
                thread_root=thread_root,
                from_bot_id=from_bot.id if from_bot is not None else None,
            )
            out.append(Delivered(bot_id, names[bot_id], queued))
        return out

    @staticmethod
    async def _note(uow: UnitOfWork, group_id: uuid.UUID, text: str) -> None:
        await uow.groups.add_message(
            uuid.uuid4(),
            group_id,
            author_kind="system",
            author_bot_id=None,
            author_name="",
            content=text,
        )

    # --- bots messaging bots -----------------------------------------------------------

    async def message_bot(
        self,
        sender: Any,
        recipient_name: str,
        text: str,
        *,
        run_id: Any,
        step: int,
        hops: int,
        handoff: bool = False,
    ) -> Delivered:
        """Put a message in another bot's conversation and queue it a wake."""
        async with self._uow.transaction() as uow:
            bots = await uow.bots.list_for(sender.organization_id)
            wanted = recipient_name.strip().lstrip("@").lower()
            recipient = next((b for b in bots if b.name.lower() == wanted), None)
            if recipient is None or recipient.id == sender.id:
                known = ", ".join(b.name for b in bots if b.id != sender.id) or "none"
                raise GroupError(f"there is no other bot called {recipient_name!r} (bots: {known})")
            if hops > MAX_HOPS:
                raise GroupError("this has been passed between bots too many times; reply instead")
            if await uow.groups.sent_today(sender.id) >= DAILY_BOT_MESSAGES:
                raise GroupError(
                    f"you have sent other bots {DAILY_BOT_MESSAGES} messages today, the most "
                    "you may; tell your person instead"
                )
            mid = message_id(run_id, step, "bot_message")
            await uow.bots.add_message(
                mid,
                recipient.id,
                role="user",
                content=text,
                payload={
                    "from_bot_id": str(sender.id),
                    "from_bot_name": sender.name,
                    "peer": True,
                    "handoff": handoff,
                },
            )
            queued = await uow.groups.add_wake(
                wake_id("message", mid),
                recipient.id,
                kind="message",
                hops=hops,
                from_bot_id=sender.id,
                message_id=mid,
                expects_reply=not handoff,
                handoff=handoff,
            )
            await uow.bots.set_flags(recipient.id, unread=True)
        return Delivered(recipient.id, recipient.name, queued)

    async def relay_reply(self, wake: WakeRow, bot: Any, text: str, *, run_id: Any) -> bool:
        """The answer to a bot's message goes back to it, and wakes it. A relayed reply
        asks for nothing back, so a conversation between two bots ends here."""
        if not wake.expects_reply or wake.from_bot_id is None:
            return False
        async with self._uow.transaction() as uow:
            sender = await uow.bots.get(wake.from_bot_id)
            if sender is None:
                return False
            mid = uuid.uuid5(uuid.NAMESPACE_URL, f"botreply:{wake.id}")
            await uow.bots.add_message(
                mid,
                sender.id,
                role="user",
                content=text,
                payload={
                    "from_bot_id": str(bot.id),
                    "from_bot_name": bot.name,
                    "peer": True,
                    "in_reply_to": str(wake.message_id) if wake.message_id else None,
                },
                run_id=uuid.UUID(str(run_id)),
            )
            if wake.hops + 1 <= MAX_HOPS:
                await uow.groups.add_wake(
                    wake_id("reply", wake.id),
                    sender.id,
                    kind="message",
                    hops=wake.hops + 1,
                    from_bot_id=bot.id,
                    message_id=mid,
                    expects_reply=False,
                )
            await uow.bots.set_flags(sender.id, unread=True)
        return True

    async def peers(self, bot: Any) -> list[Any]:
        """The person's other bots — everyone a bot may `message_bot` — not its own
        helpers, which it reaches with `ask_bot`."""
        async with self._uow() as uow:
            bots = await uow.bots.list_for(bot.organization_id)
        return [b for b in bots if b.id != bot.id and getattr(b, "parent_bot_id", None) != bot.id]

    async def wake(self, wake_id_: uuid.UUID) -> WakeRow | None:
        async with self._uow() as uow:
            return await uow.groups.get_wake(wake_id_)

    async def message(self, message_id_: uuid.UUID) -> GroupMessageRow | None:
        async with self._uow() as uow:
            return await uow.groups.get_message(message_id_)

    async def post_system(self, group_id: uuid.UUID, text: str) -> None:
        async with self._uow.transaction() as uow:
            await self._note(uow, group_id, text)


def _said(row: GroupMessageRow) -> Said:
    if row.author_kind == "bot":
        return Said(
            "bot",
            row.content,
            {
                "author_bot_id": str(row.author_bot_id),
                "author_name": row.author_name,
                "thread": row.thread_root is not None,
            },
        )
    return Said(
        "user",
        row.content,
        {
            "author_name": "" if row.author_kind == "person" else "(note)",
            "thread": row.thread_root is not None,
        },
    )
