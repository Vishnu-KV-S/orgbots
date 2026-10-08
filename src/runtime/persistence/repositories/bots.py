"""Bots, their conversations, rules, pending actions, memories and briefs.

See migrations 037 (bots) and 040 (memory and brief)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.ids import OrganizationId

_BOT_COLUMNS = """
    id, organization_id, actor_name, name, label, description, avatar, brief, brief_locked,
    brief_rev, pinned, hidden, unread, needs_attention, stop_requested, turn, last_run_id,
    duplicated_from, parent_bot_id, created_by, appearance, created_at, updated_at
"""

EDITABLE = frozenset(
    {
        "name",
        "label",
        "description",
        "avatar",
        "brief_locked",
        "pinned",
        "hidden",
        "appearance",
    }
)
_JSON_FIELDS = frozenset({"appearance"})


@dataclass(frozen=True, slots=True)
class BotRow:
    id: uuid.UUID
    organization_id: OrganizationId
    actor_name: str
    name: str
    label: str
    description: str
    avatar: str
    brief: dict[str, Any]
    """The job brief (`BotBrief`). Changed only through `set_brief`, which keeps the
    revision history; never through `update`."""
    brief_locked: bool
    brief_rev: int
    pinned: bool
    hidden: bool
    unread: bool
    needs_attention: bool
    stop_requested: bool
    turn: int
    last_run_id: uuid.UUID | None
    duplicated_from: uuid.UUID | None
    parent_bot_id: uuid.UUID | None
    created_by: str
    appearance: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BotMemoryRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    kind: str
    content: str
    importance: int
    pinned: bool
    source_kind: str
    source_name: str
    recall_count: int
    last_recalled_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BriefRevisionRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    rev: int
    brief: dict[str, Any]
    editor_kind: str
    editor_bot_id: uuid.UUID | None
    editor_name: str
    reason: str
    changed: list[str]
    created_at: datetime


_MEMORY_COLUMNS = """
    id, bot_id, kind, content, importance, pinned, source_kind, source_name, recall_count,
    last_recalled_at, created_at, updated_at
"""


def _memory(row: Any) -> BotMemoryRow:
    return BotMemoryRow(
        id=row.id,
        bot_id=row.bot_id,
        kind=row.kind,
        content=row.content,
        importance=int(row.importance),
        pinned=bool(row.pinned),
        source_kind=row.source_kind,
        source_name=row.source_name,
        recall_count=int(row.recall_count),
        last_recalled_at=row.last_recalled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: Any) -> BriefRevisionRow:
    return BriefRevisionRow(
        id=row.id,
        bot_id=row.bot_id,
        rev=int(row.rev),
        brief=dict(row.brief or {}),
        editor_kind=row.editor_kind,
        editor_bot_id=row.editor_bot_id,
        editor_name=row.editor_name,
        reason=row.reason,
        changed=list(row.changed or []),
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class BotMessageRow:
    id: uuid.UUID
    seq: int
    bot_id: uuid.UUID
    role: str
    content: str
    payload: dict[str, Any]
    run_id: uuid.UUID | None
    reply_to: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BotRuleRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    action_type: str
    host: str
    decision: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BotPendingRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    run_id: uuid.UUID | None
    action: dict[str, Any]
    reason: str
    status: str
    scope: str | None
    created_at: datetime
    decided_at: datetime | None


def _bot(row: Any) -> BotRow:
    return BotRow(
        id=row.id,
        organization_id=OrganizationId(row.organization_id),
        actor_name=row.actor_name,
        name=row.name,
        label=row.label,
        description=row.description,
        avatar=row.avatar,
        brief=dict(row.brief or {}),
        brief_locked=bool(row.brief_locked),
        brief_rev=int(row.brief_rev),
        pinned=bool(row.pinned),
        hidden=bool(row.hidden),
        unread=bool(row.unread),
        needs_attention=bool(row.needs_attention),
        stop_requested=bool(row.stop_requested),
        turn=int(row.turn),
        last_run_id=row.last_run_id,
        duplicated_from=row.duplicated_from,
        parent_bot_id=row.parent_bot_id,
        created_by=row.created_by,
        appearance=dict(row.appearance or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message(row: Any) -> BotMessageRow:
    return BotMessageRow(
        id=row.id,
        seq=int(row.seq),
        bot_id=row.bot_id,
        role=row.role,
        content=row.content,
        payload=dict(row.payload or {}),
        run_id=row.run_id,
        reply_to=row.reply_to,
        created_at=row.created_at,
    )


def _pending(row: Any) -> BotPendingRow:
    return BotPendingRow(
        id=row.id,
        bot_id=row.bot_id,
        run_id=row.run_id,
        action=dict(row.action or {}),
        reason=row.reason,
        status=row.status,
        scope=row.scope,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class BotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- bots ----------------------------------------------------------------------

    async def create(
        self,
        bot_id: uuid.UUID,
        organization_id: OrganizationId,
        *,
        actor_name: str,
        name: str,
        label: str = "",
        description: str = "",
        avatar: str = "",
        duplicated_from: uuid.UUID | None = None,
        parent_bot_id: uuid.UUID | None = None,
        created_by: str = "person",
        appearance: dict[str, Any] | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bots (id, organization_id, actor_name, name, label, description,
                                  avatar, duplicated_from, parent_bot_id, created_by,
                                  appearance)
                VALUES (:id, :org, :actor, :name, :label, :description,
                        :avatar, :dup, :parent, :created_by, CAST(:appearance AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": bot_id,
                "org": organization_id,
                "actor": actor_name,
                "name": name,
                "label": label,
                "description": description,
                "avatar": avatar,
                "dup": duplicated_from,
                "parent": parent_bot_id,
                "created_by": created_by,
                "appearance": json.dumps(appearance or {}),
            },
        )

    async def get(self, bot_id: uuid.UUID) -> BotRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = :id AND deleted_at IS NULL"),
                {"id": bot_id},
            )
        ).one_or_none()
        return None if row is None else _bot(row)

    async def get_by_actor(self, organization_id: OrganizationId, actor_name: str) -> BotRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_BOT_COLUMNS} FROM bots WHERE organization_id = :org "
                    "AND actor_name = :actor AND deleted_at IS NULL"
                ),
                {"org": organization_id, "actor": actor_name},
            )
        ).one_or_none()
        return None if row is None else _bot(row)

    async def list_for(self, organization_id: OrganizationId) -> list[BotRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_BOT_COLUMNS} FROM bots WHERE organization_id = :org "
                    "AND deleted_at IS NULL ORDER BY pinned DESC, updated_at DESC"
                ),
                {"org": organization_id},
            )
        ).all()
        return [_bot(r) for r in rows]

    async def update(self, bot_id: uuid.UUID, fields: dict[str, Any]) -> None:
        unknown = set(fields) - EDITABLE
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(
            f"{name} = CAST(:{name} AS jsonb)" if name in _JSON_FIELDS else f"{name} = :{name}"
            for name in sorted(fields)
        )
        params = {
            name: json.dumps(value) if name in _JSON_FIELDS else value
            for name, value in fields.items()
        }
        await self._s.execute(
            text(f"UPDATE bots SET {assignments}, updated_at = now() WHERE id = :id"),
            {**params, "id": bot_id},
        )

    async def set_flags(self, bot_id: uuid.UUID, **flags: Any) -> None:
        allowed = {"unread", "needs_attention", "stop_requested", "last_run_id"}
        unknown = set(flags) - allowed
        if unknown:
            raise ValueError(f"not a flag: {sorted(unknown)}")
        if not flags:
            return
        assignments = ", ".join(f"{name} = :{name}" for name in sorted(flags))
        await self._s.execute(
            text(f"UPDATE bots SET {assignments} WHERE id = :id"), {**flags, "id": bot_id}
        )

    async def touch(self, bot_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bots SET updated_at = now() WHERE id = :id"), {"id": bot_id}
        )

    async def children(self, bot_id: uuid.UUID) -> list[BotRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_BOT_COLUMNS} FROM bots WHERE parent_bot_id = :id "
                    "AND deleted_at IS NULL ORDER BY created_at"
                ),
                {"id": bot_id},
            )
        ).all()
        return [_bot(r) for r in rows]

    async def descendants(self, bot_id: uuid.UUID) -> list[uuid.UUID]:
        """Every live bot below this one, at any depth, deepest last."""
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE tree AS (
                        SELECT id, 1 AS depth FROM bots
                         WHERE parent_bot_id = :id AND deleted_at IS NULL
                        UNION ALL
                        SELECT b.id, tree.depth + 1 FROM bots b
                          JOIN tree ON b.parent_bot_id = tree.id
                         WHERE b.deleted_at IS NULL AND tree.depth < 16
                    )
                    SELECT id FROM tree ORDER BY depth
                    """
                ),
                {"id": bot_id},
            )
        ).all()
        return [r.id for r in rows]

    async def depth(self, bot_id: uuid.UUID) -> int:
        """0 for a person's bot, 1 for its helper, and so on."""
        row = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE up AS (
                        SELECT id, parent_bot_id, 0 AS depth FROM bots WHERE id = :id
                        UNION ALL
                        SELECT b.id, b.parent_bot_id, up.depth + 1 FROM bots b
                          JOIN up ON b.id = up.parent_bot_id
                         WHERE up.depth < 16
                    )
                    SELECT max(depth) AS depth FROM up
                    """
                ),
                {"id": bot_id},
            )
        ).one()
        return int(row.depth or 0)

    async def reparent_children(self, bot_id: uuid.UUID, new_parent: uuid.UUID | None) -> int:
        """Move a bot's direct helpers up to its own parent (or to the top level)."""
        result = await self._s.execute(
            text(
                "UPDATE bots SET parent_bot_id = :new, updated_at = now() "
                "WHERE parent_bot_id = :id AND deleted_at IS NULL"
            ),
            {"id": bot_id, "new": new_parent},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def bump_turn(self, bot_id: uuid.UUID) -> int:
        """Start a new turn: clears Stop, and supersedes any run still working."""
        row = (
            await self._s.execute(
                text(
                    "UPDATE bots SET turn = turn + 1, stop_requested = false, "
                    "needs_attention = false, updated_at = now() WHERE id = :id RETURNING turn"
                ),
                {"id": bot_id},
            )
        ).one()
        return int(row.turn)

    async def soft_delete(self, bot_id: uuid.UUID) -> None:
        """Free the bot's name and stop its actor admitting runs. The actor row stays —
        its runs, ledger rows and audit trail still point at it."""
        await self._s.execute(
            text(
                """
                WITH gone AS (
                    UPDATE bots SET deleted_at = now() WHERE id = :id
                    RETURNING organization_id, actor_name
                )
                UPDATE actors a SET active = false
                  FROM gone WHERE a.organization_id = gone.organization_id
                              AND a.name = gone.actor_name
                """
            ),
            {"id": bot_id},
        )

    # --- the brief -----------------------------------------------------------------

    async def set_brief(
        self,
        bot_id: uuid.UUID,
        brief: dict[str, Any],
        *,
        revision_id: uuid.UUID,
        editor_kind: str,
        editor_bot_id: uuid.UUID | None = None,
        editor_name: str = "",
        reason: str = "",
        changed: list[str] | None = None,
    ) -> int | None:
        """Write a new brief and its revision row, in one statement.

        Idempotent per `revision_id`: a replayed step that already wrote its revision
        returns None and changes nothing. Returns the new revision number otherwise.
        """
        exists = (
            await self._s.execute(
                text("SELECT 1 FROM bot_brief_revisions WHERE id = :id"), {"id": revision_id}
            )
        ).one_or_none()
        if exists is not None:
            return None
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE bots SET brief = CAST(:brief AS jsonb), brief_rev = brief_rev + 1,
                                    updated_at = now()
                     WHERE id = :id
                    RETURNING brief_rev
                    """
                ),
                {"id": bot_id, "brief": json.dumps(brief)},
            )
        ).one()
        rev = int(row.brief_rev)
        await self._s.execute(
            text(
                """
                INSERT INTO bot_brief_revisions (id, bot_id, rev, brief, editor_kind,
                                                 editor_bot_id, editor_name, reason, changed)
                VALUES (:id, :bot, :rev, CAST(:brief AS jsonb), :kind, :editor_bot,
                        :editor_name, :reason, CAST(:changed AS jsonb))
                """
            ),
            {
                "id": revision_id,
                "bot": bot_id,
                "rev": rev,
                "brief": json.dumps(brief),
                "kind": editor_kind,
                "editor_bot": editor_bot_id,
                "editor_name": editor_name,
                "reason": reason,
                "changed": json.dumps(changed or []),
            },
        )
        return rev

    async def brief_revisions(self, bot_id: uuid.UUID, limit: int = 50) -> list[BriefRevisionRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, bot_id, rev, brief, editor_kind, editor_bot_id, editor_name,
                           reason, changed, created_at
                      FROM bot_brief_revisions WHERE bot_id = :bot
                     ORDER BY rev DESC LIMIT :limit
                    """
                ),
                {"bot": bot_id, "limit": limit},
            )
        ).all()
        return [_revision(r) for r in rows]

    async def brief_revision(self, bot_id: uuid.UUID, rev: int) -> BriefRevisionRow | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, bot_id, rev, brief, editor_kind, editor_bot_id, editor_name,
                           reason, changed, created_at
                      FROM bot_brief_revisions WHERE bot_id = :bot AND rev = :rev
                    """
                ),
                {"bot": bot_id, "rev": rev},
            )
        ).one_or_none()
        return None if row is None else _revision(row)

    # --- memory --------------------------------------------------------------------

    async def memories(self, bot_id: uuid.UUID) -> list[BotMemoryRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_MEMORY_COLUMNS} FROM bot_memories WHERE bot_id = :bot "
                    "ORDER BY created_at"
                ),
                {"bot": bot_id},
            )
        ).all()
        return [_memory(r) for r in rows]

    async def get_memory(self, bot_id: uuid.UUID, memory_id: uuid.UUID) -> BotMemoryRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_MEMORY_COLUMNS} FROM bot_memories WHERE id = :id AND bot_id = :bot"
                ),
                {"id": memory_id, "bot": bot_id},
            )
        ).one_or_none()
        return None if row is None else _memory(row)

    async def add_memory(
        self,
        memory_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        kind: str,
        content: str,
        importance: int = 3,
        pinned: bool = False,
        source_kind: str = "self",
        source_name: str = "",
    ) -> bool:
        """Insert one memory. False when it already existed (a replayed step)."""
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_memories (id, bot_id, kind, content, importance, pinned,
                                          source_kind, source_name)
                VALUES (:id, :bot, :kind, :content, :importance, :pinned, :source_kind,
                        :source_name)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": memory_id,
                "bot": bot_id,
                "kind": kind,
                "content": content,
                "importance": importance,
                "pinned": pinned,
                "source_kind": source_kind,
                "source_name": source_name,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def update_memory(
        self, bot_id: uuid.UUID, memory_id: uuid.UUID, fields: dict[str, Any]
    ) -> bool:
        allowed = {"kind", "content", "importance", "pinned", "source_kind", "source_name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return True
        assignments = ", ".join(f"{name} = :{name}" for name in sorted(fields))
        result = await self._s.execute(
            text(
                f"UPDATE bot_memories SET {assignments}, updated_at = now() "
                "WHERE id = :id AND bot_id = :bot"
            ),
            {**fields, "id": memory_id, "bot": bot_id},
        )
        return bool(getattr(result, "rowcount", 0))

    async def reinforce_memory(
        self, bot_id: uuid.UUID, memory_id: uuid.UUID, importance: int
    ) -> None:
        """Saving what is already known: the memory gets more important, and fresh."""
        await self._s.execute(
            text(
                """
                UPDATE bot_memories
                   SET importance = GREATEST(importance, :importance),
                       recall_count = recall_count + 1, last_recalled_at = now(),
                       updated_at = now()
                 WHERE id = :id AND bot_id = :bot
                """
            ),
            {"id": memory_id, "bot": bot_id, "importance": importance},
        )

    async def mark_recalled(self, bot_id: uuid.UUID, memory_ids: list[uuid.UUID]) -> None:
        if not memory_ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE bot_memories
                   SET recall_count = recall_count + 1, last_recalled_at = now()
                 WHERE bot_id = :bot AND id = ANY(:ids)
                """
            ),
            {"bot": bot_id, "ids": memory_ids},
        )

    async def delete_memories(self, bot_id: uuid.UUID, memory_ids: list[uuid.UUID]) -> int:
        if not memory_ids:
            return 0
        result = await self._s.execute(
            text("DELETE FROM bot_memories WHERE bot_id = :bot AND id = ANY(:ids)"),
            {"bot": bot_id, "ids": memory_ids},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def clear_memories(self, bot_id: uuid.UUID) -> int:
        result = await self._s.execute(
            text("DELETE FROM bot_memories WHERE bot_id = :bot"), {"bot": bot_id}
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def memory_counts(self, organization_id: OrganizationId) -> dict[uuid.UUID, int]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT m.bot_id, count(*) AS n FROM bot_memories m
                      JOIN bots b ON b.id = m.bot_id
                     WHERE b.organization_id = :org AND b.deleted_at IS NULL
                     GROUP BY m.bot_id
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.bot_id: int(r.n) for r in rows}

    async def search_bot_messages(
        self, bot_id: uuid.UUID, query: str, limit: int = 6
    ) -> list[BotMessageRow]:
        """One bot's own past conversation — `recall` reaching past its context window."""
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, seq, bot_id, role, content, payload, run_id, reply_to,
                           created_at
                      FROM bot_messages
                     WHERE bot_id = :bot AND role IN ('user', 'bot') AND content ILIKE :q
                     ORDER BY seq DESC
                     LIMIT :limit
                    """
                ),
                {"bot": bot_id, "q": f"%{_escape_like(query)}%", "limit": limit},
            )
        ).all()
        return [_message(r) for r in rows]

    # --- messages ------------------------------------------------------------------

    async def add_message(
        self,
        message_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
        reply_to: uuid.UUID | None = None,
    ) -> bool:
        """Insert one message. False when it already existed (a replayed node)."""
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_messages (id, bot_id, role, content, payload, run_id, reply_to)
                VALUES (:id, :bot, :role, :content, CAST(:payload AS jsonb), :run, :reply)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": message_id,
                "bot": bot_id,
                "role": role,
                "content": content,
                "payload": json.dumps(payload or {}, default=str),
                "run": run_id,
                "reply": reply_to,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def messages(
        self, bot_id: uuid.UUID, *, after_seq: int = 0, limit: int = 500
    ) -> list[BotMessageRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, seq, bot_id, role, content, payload, run_id, reply_to, created_at
                      FROM bot_messages
                     WHERE bot_id = :bot AND seq > :after
                     ORDER BY seq ASC
                     LIMIT :limit
                    """
                ),
                {"bot": bot_id, "after": after_seq, "limit": limit},
            )
        ).all()
        return [_message(r) for r in rows]

    async def recent_conversation(self, bot_id: uuid.UUID, limit: int) -> list[BotMessageRow]:
        """The last `limit` person/bot messages, oldest first. Activity is excluded: a
        run reads what was *said*, not every click of every earlier run."""
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT * FROM (
                        SELECT id, seq, bot_id, role, content, payload, run_id, reply_to,
                               created_at
                          FROM bot_messages
                         WHERE bot_id = :bot AND role IN ('user', 'bot')
                         ORDER BY seq DESC
                         LIMIT :limit
                    ) recent ORDER BY seq ASC
                    """
                ),
                {"bot": bot_id, "limit": limit},
            )
        ).all()
        return [_message(r) for r in rows]

    async def last_message_per_bot(
        self, organization_id: OrganizationId
    ) -> dict[uuid.UUID, BotMessageRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT DISTINCT ON (m.bot_id)
                           m.id, m.seq, m.bot_id, m.role, m.content, m.payload, m.run_id,
                           m.reply_to, m.created_at
                      FROM bot_messages m JOIN bots b ON b.id = m.bot_id
                     WHERE b.organization_id = :org AND m.role IN ('user', 'bot', 'approval')
                     ORDER BY m.bot_id, m.seq DESC
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.bot_id: _message(r) for r in rows}

    async def search(
        self, organization_id: OrganizationId, query: str, limit: int = 30
    ) -> list[BotMessageRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT m.id, m.seq, m.bot_id, m.role, m.content, m.payload, m.run_id,
                           m.reply_to, m.created_at
                      FROM bot_messages m JOIN bots b ON b.id = m.bot_id
                     WHERE b.organization_id = :org AND b.deleted_at IS NULL
                       AND m.role IN ('user', 'bot')
                       AND m.content ILIKE :q
                     ORDER BY m.seq DESC
                     LIMIT :limit
                    """
                ),
                {"org": organization_id, "q": f"%{_escape_like(query)}%", "limit": limit},
            )
        ).all()
        return [_message(r) for r in rows]

    # --- rules ---------------------------------------------------------------------

    async def rules(self, bot_id: uuid.UUID) -> list[BotRuleRow]:
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, bot_id, action_type, host, decision, created_at "
                    "FROM bot_rules WHERE bot_id = :bot ORDER BY created_at"
                ),
                {"bot": bot_id},
            )
        ).all()
        return [
            BotRuleRow(
                id=r.id,
                bot_id=r.bot_id,
                action_type=r.action_type,
                host=r.host,
                decision=r.decision,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def put_rule(
        self, bot_id: uuid.UUID, action_type: str, host: str, decision: str
    ) -> uuid.UUID:
        rule_id = uuid.uuid4()
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO bot_rules (id, bot_id, action_type, host, decision)
                    VALUES (:id, :bot, :action, :host, :decision)
                    ON CONFLICT ON CONSTRAINT uq_bot_rule
                    DO UPDATE SET decision = EXCLUDED.decision
                    RETURNING id
                    """
                ),
                {
                    "id": rule_id,
                    "bot": bot_id,
                    "action": action_type,
                    "host": host,
                    "decision": decision,
                },
            )
        ).one()
        return uuid.UUID(str(row.id))

    async def delete_rule(self, bot_id: uuid.UUID, rule_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM bot_rules WHERE id = :id AND bot_id = :bot"),
            {"id": rule_id, "bot": bot_id},
        )

    async def copy_rules(self, source: uuid.UUID, target: uuid.UUID) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_rules (id, bot_id, action_type, host, decision)
                SELECT gen_random_uuid(), :target, action_type, host, decision
                  FROM bot_rules WHERE bot_id = :source
                """
            ),
            {"source": source, "target": target},
        )

    # --- pending actions -----------------------------------------------------------

    async def add_pending(
        self,
        pending_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None,
        action: dict[str, Any],
        reason: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_pending_actions (id, bot_id, run_id, action, reason)
                VALUES (:id, :bot, :run, CAST(:action AS jsonb), :reason)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": pending_id,
                "bot": bot_id,
                "run": run_id,
                "action": json.dumps(action, default=str),
                "reason": reason,
            },
        )

    async def get_pending(self, pending_id: uuid.UUID) -> BotPendingRow | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT id, bot_id, run_id, action, reason, status, scope, created_at, "
                    "decided_at FROM bot_pending_actions WHERE id = :id"
                ),
                {"id": pending_id},
            )
        ).one_or_none()
        return None if row is None else _pending(row)

    async def live_pending(self, bot_id: uuid.UUID) -> list[BotPendingRow]:
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, bot_id, run_id, action, reason, status, scope, created_at, "
                    "decided_at FROM bot_pending_actions "
                    "WHERE bot_id = :bot AND status = 'pending' ORDER BY created_at"
                ),
                {"bot": bot_id},
            )
        ).all()
        return [_pending(r) for r in rows]

    async def decide_pending(
        self, pending_id: uuid.UUID, *, status: str, scope: str | None
    ) -> bool:
        """Conditional on still being pending, so two clicks cannot both win."""
        result = await self._s.execute(
            text(
                """
                UPDATE bot_pending_actions
                   SET status = :status, scope = :scope, decided_at = now()
                 WHERE id = :id AND status = 'pending'
                """
            ),
            {"id": pending_id, "status": status, "scope": scope},
        )
        return bool(getattr(result, "rowcount", 0))

    async def expire_pending(self, bot_id: uuid.UUID) -> None:
        """A new instruction from the person supersedes whatever was waiting."""
        await self._s.execute(
            text(
                "UPDATE bot_pending_actions SET status = 'expired', decided_at = now() "
                "WHERE bot_id = :bot AND status = 'pending'"
            ),
            {"bot": bot_id},
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
