"""The organization's X account, people's linked X accounts, link codes and mentions
seen. See migration 054."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_NOBODY = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class XAccountRow:
    organization_id: uuid.UUID
    handle: str
    x_user_id: str
    read_key_id: str
    read_nonce: bytes
    read_ciphertext: bytes
    post_key_id: str | None
    post_nonce: bytes | None
    post_ciphertext: bytes | None
    since_id: str | None
    enabled: bool
    last_error: str
    last_polled_at: dt.datetime | None
    updated_at: dt.datetime

    def __repr__(self) -> str:
        return f"XAccountRow(handle={self.handle!r})"


@dataclass(frozen=True, slots=True)
class XLinkRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    member_id: uuid.UUID | None
    x_user_id: str
    handle: str
    bot_id: uuid.UUID | None
    created_at: dt.datetime


_ACCOUNT = (
    "organization_id, handle, x_user_id, read_key_id, read_nonce, read_ciphertext, "
    "post_key_id, post_nonce, post_ciphertext, since_id, enabled, last_error, "
    "last_polled_at, updated_at"
)
_LINK = "id, organization_id, member_id, x_user_id, handle, bot_id, created_at"


class XRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- the organization's account ----------------------------------------------------

    async def account(self, organization_id: uuid.UUID) -> XAccountRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_ACCOUNT} FROM x_accounts WHERE organization_id = :org"),
                {"org": organization_id},
            )
        ).first()
        return XAccountRow(*row) if row else None

    async def enabled_accounts(self) -> list[XAccountRow]:
        rows = await self._s.execute(text(f"SELECT {_ACCOUNT} FROM x_accounts WHERE enabled"))
        return [XAccountRow(*r) for r in rows]

    async def save_account(
        self,
        organization_id: uuid.UUID,
        *,
        handle: str,
        x_user_id: str,
        read: tuple[str, bytes, bytes] | None,
        post: tuple[str, bytes, bytes] | None,
        clear_post: bool,
        enabled: bool,
    ) -> None:
        """`read`/`post` None keeps what is saved (`clear_post` drops the post token)."""
        rk, rn, rc = read or (None, None, None)
        pk, pn, pc = post or (None, None, None)
        await self._s.execute(
            text(
                """
                INSERT INTO x_accounts (organization_id, handle, x_user_id, read_key_id,
                                        read_nonce, read_ciphertext, post_key_id, post_nonce,
                                        post_ciphertext, enabled)
                VALUES (:org, :handle, :uid, :rk, :rn, :rc, :pk, :pn, :pc, :enabled)
                ON CONFLICT (organization_id) DO UPDATE SET
                    handle = EXCLUDED.handle,
                    since_id = CASE WHEN x_accounts.x_user_id = EXCLUDED.x_user_id
                                    THEN x_accounts.since_id END,
                    x_user_id = EXCLUDED.x_user_id,
                    read_key_id = COALESCE(EXCLUDED.read_key_id, x_accounts.read_key_id),
                    read_nonce = COALESCE(EXCLUDED.read_nonce, x_accounts.read_nonce),
                    read_ciphertext = COALESCE(EXCLUDED.read_ciphertext,
                                               x_accounts.read_ciphertext),
                    post_key_id = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.post_key_id, x_accounts.post_key_id) END,
                    post_nonce = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.post_nonce, x_accounts.post_nonce) END,
                    post_ciphertext = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.post_ciphertext, x_accounts.post_ciphertext) END,
                    enabled = EXCLUDED.enabled, last_error = '', updated_at = now()
                """
            ),
            {
                "org": organization_id,
                "handle": handle,
                "uid": x_user_id,
                "rk": rk,
                "rn": rn,
                "rc": rc,
                "pk": pk,
                "pn": pn,
                "pc": pc,
                "clear": clear_post,
                "enabled": enabled,
            },
        )

    async def delete_account(self, organization_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM x_accounts WHERE organization_id = :org"), {"org": organization_id}
        )

    async def polled(self, organization_id: uuid.UUID, *, since_id: str | None, error: str) -> None:
        await self._s.execute(
            text(
                "UPDATE x_accounts SET since_id = COALESCE(:since, since_id), "
                "last_error = :error, last_polled_at = now() WHERE organization_id = :org"
            ),
            {"org": organization_id, "since": since_id, "error": error[:500]},
        )

    # --- people's links ------------------------------------------------------------------

    async def link_for(
        self, organization_id: uuid.UUID, member_id: uuid.UUID | None
    ) -> XLinkRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_LINK} FROM x_links WHERE organization_id = :org "
                    "AND coalesce(member_id, :nobody) = :member"
                ),
                {"org": organization_id, "member": member_id or _NOBODY, "nobody": _NOBODY},
            )
        ).first()
        return XLinkRow(*row) if row else None

    async def link_by_account(self, organization_id: uuid.UUID, x_user_id: str) -> XLinkRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_LINK} FROM x_links WHERE organization_id = :org AND x_user_id = :uid"
                ),
                {"org": organization_id, "uid": x_user_id},
            )
        ).first()
        return XLinkRow(*row) if row else None

    async def link(
        self,
        organization_id: uuid.UUID,
        member_id: uuid.UUID | None,
        *,
        x_user_id: str,
        handle: str,
    ) -> None:
        """Link an X account to a person — replacing their old link, and taking the X
        account from anyone it was linked to (it has just proved whose it is)."""
        await self._s.execute(
            text(
                "DELETE FROM x_links WHERE organization_id = :org AND (x_user_id = :uid "
                "OR coalesce(member_id, :nobody) = :member)"
            ),
            {
                "org": organization_id,
                "uid": x_user_id,
                "member": member_id or _NOBODY,
                "nobody": _NOBODY,
            },
        )
        await self._s.execute(
            text(
                "INSERT INTO x_links (id, organization_id, member_id, x_user_id, handle) "
                "VALUES (:id, :org, :member, :uid, :handle)"
            ),
            {
                "id": uuid.uuid4(),
                "org": organization_id,
                "member": member_id,
                "uid": x_user_id,
                "handle": handle,
            },
        )

    async def set_bot(self, link_id: uuid.UUID, bot_id: uuid.UUID | None) -> None:
        await self._s.execute(
            text("UPDATE x_links SET bot_id = :bot WHERE id = :id"), {"id": link_id, "bot": bot_id}
        )

    async def unlink(self, organization_id: uuid.UUID, member_id: uuid.UUID | None) -> bool:
        done = await self._s.execute(
            text(
                "DELETE FROM x_links WHERE organization_id = :org "
                "AND coalesce(member_id, :nobody) = :member"
            ),
            {"org": organization_id, "member": member_id or _NOBODY, "nobody": _NOBODY},
        )
        return bool(getattr(done, "rowcount", 0))

    # --- link codes -----------------------------------------------------------------------

    async def add_code(
        self,
        code: str,
        organization_id: uuid.UUID,
        member_id: uuid.UUID | None,
        expires_at: dt.datetime,
    ) -> None:
        await self._s.execute(
            text(
                "DELETE FROM x_link_codes WHERE organization_id = :org "
                "AND (coalesce(member_id, :nobody) = :member OR expires_at < now())"
            ),
            {"org": organization_id, "member": member_id or _NOBODY, "nobody": _NOBODY},
        )
        await self._s.execute(
            text(
                "INSERT INTO x_link_codes (code, organization_id, member_id, expires_at) "
                "VALUES (:code, :org, :member, :expires)"
            ),
            {"code": code, "org": organization_id, "member": member_id, "expires": expires_at},
        )

    async def take_code(
        self, organization_id: uuid.UUID, code: str
    ) -> tuple[bool, uuid.UUID | None]:
        """Spend a live code: `(found, member_id)`."""
        row = (
            await self._s.execute(
                text(
                    "DELETE FROM x_link_codes WHERE organization_id = :org AND code = :code "
                    "AND expires_at > now() RETURNING member_id"
                ),
                {"org": organization_id, "code": code},
            )
        ).first()
        return (row is not None, row.member_id if row else None)

    # --- mentions -------------------------------------------------------------------------

    async def seen(self, post_id: str) -> bool:
        found = await self._s.scalar(
            text("SELECT 1 FROM x_mentions WHERE post_id = :id"), {"id": post_id}
        )
        return found is not None

    async def record(
        self,
        post_id: str,
        organization_id: uuid.UUID,
        *,
        author_id: str,
        author_handle: str,
        outcome: str,
        bot_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        note: str = "",
    ) -> bool:
        """False when the post was already recorded (another poller took it)."""
        done = await self._s.execute(
            text(
                """
                INSERT INTO x_mentions (post_id, organization_id, author_id, author_handle,
                                        outcome, bot_id, message_id, note)
                VALUES (:id, :org, :author, :handle, :outcome, :bot, :message, :note)
                ON CONFLICT (post_id) DO NOTHING
                """
            ),
            {
                "id": post_id,
                "org": organization_id,
                "author": author_id,
                "handle": author_handle,
                "outcome": outcome,
                "bot": bot_id,
                "message": message_id,
                "note": note[:300],
            },
        )
        return bool(getattr(done, "rowcount", 0))

    async def recent(self, organization_id: uuid.UUID, limit: int = 20) -> list[dict[str, object]]:
        rows = await self._s.execute(
            text(
                "SELECT post_id, author_handle, outcome, bot_id, note, created_at "
                "FROM x_mentions WHERE organization_id = :org ORDER BY created_at DESC LIMIT :n"
            ),
            {"org": organization_id, "n": limit},
        )
        return [dict(r._mapping) for r in rows]
