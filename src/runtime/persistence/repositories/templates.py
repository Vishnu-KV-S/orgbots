"""Shared bot templates — links to a snapshot of a bot's setup. See migration 051."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ShareRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    bot_id: uuid.UUID | None
    token: str
    template: dict[str, Any]
    uses: int
    created_at: dt.datetime
    revoked_at: dt.datetime | None


_COLUMNS = "id, organization_id, bot_id, token, template, uses, created_at, revoked_at"


class TemplateShareRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        share_id: uuid.UUID,
        organization_id: uuid.UUID,
        bot_id: uuid.UUID,
        token: str,
        template: dict[str, Any],
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_template_shares (id, organization_id, bot_id, token, template)
                VALUES (:id, :org, :bot, :token, CAST(:template AS jsonb))
                """
            ),
            {
                "id": share_id,
                "org": organization_id,
                "bot": bot_id,
                "token": token,
                "template": json.dumps(template),
            },
        )

    async def by_token(self, token: str) -> ShareRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM bot_template_shares WHERE token = :t"), {"t": token}
            )
        ).first()
        return ShareRow(*row) if row else None

    async def for_bot(self, bot_id: uuid.UUID) -> list[ShareRow]:
        """The bot's links that still work, newest first."""
        rows = await self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM bot_template_shares "
                "WHERE bot_id = :b AND revoked_at IS NULL ORDER BY created_at DESC"
            ),
            {"b": bot_id},
        )
        return [ShareRow(*r) for r in rows]

    async def revoke(self, bot_id: uuid.UUID, share_id: uuid.UUID) -> bool:
        done = await self._s.execute(
            text(
                "UPDATE bot_template_shares SET revoked_at = now() "
                "WHERE id = :id AND bot_id = :b AND revoked_at IS NULL"
            ),
            {"id": share_id, "b": bot_id},
        )
        return bool(getattr(done, "rowcount", 0))

    async def used(self, share_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bot_template_shares SET uses = uses + 1 WHERE id = :id"), {"id": share_id}
        )
