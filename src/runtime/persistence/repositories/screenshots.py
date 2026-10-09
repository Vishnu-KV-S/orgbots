"""Screenshots of a bot's screen, kept for the chat. See migration 055."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ScreenshotRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    run_id: uuid.UUID | None
    kind: str
    page_url: str
    media_type: str
    data: bytes
    created_at: dt.datetime

    def __repr__(self) -> str:
        return f"ScreenshotRow(id={self.id}, kind={self.kind!r}, bytes={len(self.data)})"


class ScreenshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        screenshot_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None,
        kind: str,
        page_url: str,
        data: bytes,
        media_type: str = "image/jpeg",
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_screenshots (id, bot_id, run_id, kind, page_url, media_type,
                                             data)
                VALUES (:id, :bot, :run, :kind, :url, :media, :data)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": screenshot_id,
                "bot": bot_id,
                "run": run_id,
                "kind": kind,
                "url": page_url[:2_000],
                "media": media_type,
                "data": data,
            },
        )

    async def get(self, bot_id: uuid.UUID, screenshot_id: uuid.UUID) -> ScreenshotRow | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, bot_id, run_id, kind, page_url, media_type, data, created_at
                      FROM bot_screenshots WHERE id = :id AND bot_id = :bot
                    """
                ),
                {"id": screenshot_id, "bot": bot_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return ScreenshotRow(
            id=row.id,
            bot_id=row.bot_id,
            run_id=row.run_id,
            kind=row.kind,
            page_url=row.page_url,
            media_type=row.media_type,
            data=bytes(row.data),
            created_at=row.created_at,
        )

    async def prune(self, bot_id: uuid.UUID, *, keep: int) -> int:
        """Keep the `keep` most recent for this bot; delete the rest."""
        result = await self._s.execute(
            text(
                """
                DELETE FROM bot_screenshots
                 WHERE bot_id = :bot AND id NOT IN (
                       SELECT id FROM bot_screenshots WHERE bot_id = :bot
                        ORDER BY created_at DESC LIMIT :keep)
                """
            ),
            {"bot": bot_id, "keep": keep},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_for_bot(self, bot_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM bot_screenshots WHERE bot_id = :bot"), {"bot": bot_id}
        )
