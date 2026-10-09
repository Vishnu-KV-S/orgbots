"""Group chats, the bots' wake queue, and reactions. See migration 047."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_GROUP = "id, organization_id, name, lead_bot_id, unread, created_at, updated_at"
_MESSAGE = """
    id, seq, group_id, author_kind, author_bot_id, author_name, content, payload,
    thread_root, run_id, created_at
"""
_WAKE = """
    id, bot_id, kind, group_id, group_message_id, thread_root, from_bot_id, message_id, hops,
    expects_reply, handoff, status, run_id, detail, created_at, started_at
"""


@dataclass(frozen=True, slots=True)
class GroupRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    lead_bot_id: uuid.UUID | None
    unread: bool
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class GroupMessageRow:
    id: uuid.UUID
    seq: int
    group_id: uuid.UUID
    author_kind: str
    author_bot_id: uuid.UUID | None
    author_name: str
    content: str
    payload: dict[str, Any]
    thread_root: uuid.UUID | None
    run_id: uuid.UUID | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class WakeRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    kind: str
    group_id: uuid.UUID | None
    group_message_id: uuid.UUID | None
    thread_root: uuid.UUID | None
    from_bot_id: uuid.UUID | None
    message_id: uuid.UUID | None
    hops: int
    expects_reply: bool
    handoff: bool
    status: str
    run_id: uuid.UUID | None
    detail: str
    created_at: dt.datetime
    started_at: dt.datetime | None


def _group(r: Any) -> GroupRow:
    return GroupRow(
        id=r.id,
        organization_id=r.organization_id,
        name=r.name,
        lead_bot_id=r.lead_bot_id,
        unread=r.unread,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _message(r: Any) -> GroupMessageRow:
    return GroupMessageRow(
        id=r.id,
        seq=int(r.seq),
        group_id=r.group_id,
        author_kind=r.author_kind,
        author_bot_id=r.author_bot_id,
        author_name=r.author_name,
        content=r.content,
        payload=dict(r.payload or {}),
        thread_root=r.thread_root,
        run_id=r.run_id,
        created_at=r.created_at,
    )


def _wake(r: Any) -> WakeRow:
    return WakeRow(**{k: getattr(r, k) for k in WakeRow.__slots__})


class GroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- groups --------------------------------------------------------------------

    async def create(
        self,
        group_id: uuid.UUID,
        organization_id: uuid.UUID,
        name: str,
        members: list[uuid.UUID],
        lead: uuid.UUID | None,
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO bot_groups (id, organization_id, name, lead_bot_id) "
                "VALUES (:id, :org, :name, :lead)"
            ),
            {"id": group_id, "org": organization_id, "name": name, "lead": lead},
        )
        await self.set_members(group_id, members)

    async def set_members(self, group_id: uuid.UUID, members: list[uuid.UUID]) -> None:
        await self._s.execute(
            text("DELETE FROM bot_group_members WHERE group_id = :g AND NOT (bot_id = ANY(:m))"),
            {"g": group_id, "m": members},
        )
        for bot_id in members:
            await self._s.execute(
                text(
                    "INSERT INTO bot_group_members (group_id, bot_id) VALUES (:g, :b) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"g": group_id, "b": bot_id},
            )

    async def update(self, group_id: uuid.UUID, **fields: Any) -> None:
        allowed = {"name", "lead_bot_id", "unread"}
        if set(fields) - allowed:
            raise ValueError(f"not editable: {sorted(set(fields) - allowed)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in sorted(fields))
        await self._s.execute(
            text(f"UPDATE bot_groups SET {sets}, updated_at = now() WHERE id = :id"),
            {**fields, "id": group_id},
        )

    async def touch(self, group_id: uuid.UUID, *, unread: bool) -> None:
        await self._s.execute(
            text("UPDATE bot_groups SET updated_at = now(), unread = :u WHERE id = :id"),
            {"id": group_id, "u": unread},
        )

    async def get(self, group_id: uuid.UUID) -> GroupRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_GROUP} FROM bot_groups WHERE id = :id AND deleted_at IS NULL"),
                {"id": group_id},
            )
        ).first()
        return None if row is None else _group(row)

    async def list_for(self, organization_id: uuid.UUID) -> list[GroupRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_GROUP} FROM bot_groups WHERE organization_id = :org "
                    "AND deleted_at IS NULL ORDER BY updated_at DESC"
                ),
                {"org": organization_id},
            )
        ).all()
        return [_group(r) for r in rows]

    async def members(self, group_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
        """`(bot_id, name, label)` of live members, in the order they joined."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT b.id, b.name, b.label FROM bot_group_members m "
                    "JOIN bots b ON b.id = m.bot_id "
                    "WHERE m.group_id = :g AND b.deleted_at IS NULL ORDER BY m.added_at, b.name"
                ),
                {"g": group_id},
            )
        ).all()
        return [(r.id, r.name, r.label) for r in rows]

    async def groups_of(self, bot_id: uuid.UUID) -> list[GroupRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {', '.join('g.' + c.strip() for c in _GROUP.split(','))} "
                    "FROM bot_groups g JOIN bot_group_members m ON m.group_id = g.id "
                    "WHERE m.bot_id = :b AND g.deleted_at IS NULL"
                ),
                {"b": bot_id},
            )
        ).all()
        return [_group(r) for r in rows]

    async def soft_delete(self, group_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bot_groups SET deleted_at = now() WHERE id = :id"), {"id": group_id}
        )

    # --- messages ------------------------------------------------------------------

    async def add_message(
        self,
        message_id: uuid.UUID,
        group_id: uuid.UUID,
        *,
        author_kind: str,
        author_bot_id: uuid.UUID | None,
        author_name: str,
        content: str,
        payload: dict[str, Any] | None = None,
        thread_root: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> bool:
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_group_messages (id, group_id, author_kind, author_bot_id,
                                                author_name, content, payload, thread_root,
                                                run_id)
                VALUES (:id, :g, :kind, :bot, :name, :content, CAST(:payload AS jsonb),
                        :thread, :run)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": message_id,
                "g": group_id,
                "kind": author_kind,
                "bot": author_bot_id,
                "name": author_name,
                "content": content,
                "payload": json.dumps(payload or {}, default=str),
                "thread": thread_root,
                "run": run_id,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def get_message(self, message_id: uuid.UUID) -> GroupMessageRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_MESSAGE} FROM bot_group_messages WHERE id = :id"),
                {"id": message_id},
            )
        ).first()
        return None if row is None else _message(row)

    async def messages(self, group_id: uuid.UUID, after_seq: int = 0) -> list[GroupMessageRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_MESSAGE} FROM bot_group_messages WHERE group_id = :g "
                    "AND seq > :after ORDER BY seq LIMIT 500"
                ),
                {"g": group_id, "after": after_seq},
            )
        ).all()
        return [_message(r) for r in rows]

    async def recent(
        self, group_id: uuid.UUID, limit: int, *, thread_root: uuid.UUID | None = None
    ) -> list[GroupMessageRow]:
        """The last `limit` messages, oldest first — of the main conversation, and of a
        thread (its root and replies) when one is named."""
        clause = "thread_root IS NULL" if thread_root is None else "(id = :t OR thread_root = :t)"
        rows = (
            await self._s.execute(
                text(
                    f"SELECT * FROM (SELECT {_MESSAGE} FROM bot_group_messages "
                    f"WHERE group_id = :g AND {clause} ORDER BY seq DESC LIMIT :limit) r "
                    "ORDER BY seq"
                ),
                {"g": group_id, "t": thread_root, "limit": limit},
            )
        ).all()
        return [_message(r) for r in rows]

    async def last_message_per_group(
        self, organization_id: uuid.UUID
    ) -> dict[uuid.UUID, GroupMessageRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT DISTINCT ON (m.group_id) "
                    f"{', '.join('m.' + c.strip() for c in _MESSAGE.split(','))} "
                    "FROM bot_group_messages m JOIN bot_groups g ON g.id = m.group_id "
                    "WHERE g.organization_id = :org ORDER BY m.group_id, m.seq DESC"
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.group_id: _message(r) for r in rows}

    # --- wakes ---------------------------------------------------------------------

    async def add_wake(
        self,
        wake_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        kind: str,
        hops: int,
        group_id: uuid.UUID | None = None,
        group_message_id: uuid.UUID | None = None,
        thread_root: uuid.UUID | None = None,
        from_bot_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        expects_reply: bool = False,
        handoff: bool = False,
        status: str = "queued",
    ) -> bool:
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_wakes (id, bot_id, kind, group_id, group_message_id,
                                       thread_root, from_bot_id, message_id, hops,
                                       expects_reply, handoff, status)
                VALUES (:id, :bot, :kind, :g, :gm, :thread, :from, :m, :hops, :reply,
                        :handoff, :status)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": wake_id,
                "bot": bot_id,
                "kind": kind,
                "g": group_id,
                "gm": group_message_id,
                "thread": thread_root,
                "from": from_bot_id,
                "m": message_id,
                "hops": hops,
                "reply": expects_reply,
                "handoff": handoff,
                "status": status,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def get_wake(self, wake_id: uuid.UUID) -> WakeRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_WAKE} FROM bot_wakes WHERE id = :id"), {"id": wake_id}
            )
        ).first()
        return None if row is None else _wake(row)

    async def queued_wakes(self, limit: int = 200) -> list[WakeRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_WAKE} FROM bot_wakes WHERE status = 'queued' "
                    "ORDER BY created_at LIMIT :limit"
                ),
                {"limit": limit},
            )
        ).all()
        return [_wake(r) for r in rows]

    async def claim_wake(self, wake_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            text(
                "UPDATE bot_wakes SET status = 'started', started_at = now() "
                "WHERE id = :id AND status = 'queued'"
            ),
            {"id": wake_id},
        )
        return bool(getattr(result, "rowcount", 0))

    async def finish_wake(
        self, wake_id: uuid.UUID, *, status: str, run_id: uuid.UUID | None, detail: str = ""
    ) -> None:
        await self._s.execute(
            text(
                "UPDATE bot_wakes SET status = :status, run_id = :run, detail = :detail "
                "WHERE id = :id"
            ),
            {"id": wake_id, "status": status, "run": run_id, "detail": detail},
        )

    async def sent_today(self, bot_id: uuid.UUID) -> int:
        """Deliveries this bot set off in the last day — its allowance's use."""
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT count(*) FROM bot_wakes WHERE from_bot_id = :b "
                        "AND created_at > now() - interval '1 day'"
                    ),
                    {"b": bot_id},
                )
            ).scalar_one()
        )

    # --- reactions -------------------------------------------------------------------

    async def react(self, message_id: uuid.UUID, emoji: str, scope: str, on: bool) -> None:
        if on:
            await self._s.execute(
                text(
                    "INSERT INTO bot_reactions (message_id, emoji, scope) VALUES (:m, :e, :s) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"m": message_id, "e": emoji, "s": scope},
            )
        else:
            await self._s.execute(
                text("DELETE FROM bot_reactions WHERE message_id = :m AND emoji = :e"),
                {"m": message_id, "e": emoji},
            )

    async def reactions(self, message_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
        if not message_ids:
            return {}
        rows = (
            await self._s.execute(
                text(
                    "SELECT message_id, emoji FROM bot_reactions WHERE message_id = ANY(:ids) "
                    "ORDER BY created_at"
                ),
                {"ids": message_ids},
            )
        ).all()
        out: dict[uuid.UUID, list[str]] = {}
        for r in rows:
            out.setdefault(r.message_id, []).append(r.emoji)
        return out
