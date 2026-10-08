"""The login vault and the credential requests a bot makes. See migration 041.

Same boundary as `credentials`: this layer stores and returns *ciphertext* and holds
no key. Opening an entry is `runtime.gateway.vault`, one layer up, so a bug here leaks
nothing a database dump would not already have.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ENTRY_COLUMNS = """
    id, organization_id, host, kind, bot_id, label, kinds, key_id, nonce, ciphertext,
    auto_use, use_count, last_used_at, expires_at, created_at, updated_at
"""

_REQUEST_COLUMNS = """
    id, bot_id, run_id, host, page_url, purpose, fields, status, entry_ids, saved,
    working, created_at, decided_at
"""


@dataclass(frozen=True, slots=True)
class VaultEntryRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    host: str
    kind: str
    bot_id: uuid.UUID | None
    label: str
    kinds: tuple[str, ...]
    key_id: str
    nonce: bytes
    ciphertext: bytes
    auto_use: bool
    use_count: int
    last_used_at: dt.datetime | None
    expires_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def __repr__(self) -> str:
        return f"VaultEntryRow(id={self.id}, host={self.host!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class CredentialRequestRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    run_id: uuid.UUID | None
    host: str
    page_url: str
    purpose: str
    fields: list[dict[str, Any]]
    status: str
    entry_ids: tuple[uuid.UUID, ...]
    saved: bool
    working: dict[str, Any]
    created_at: dt.datetime
    decided_at: dt.datetime | None


def _entry(r: Any) -> VaultEntryRow:
    return VaultEntryRow(
        id=r.id,
        organization_id=r.organization_id,
        host=r.host,
        kind=r.kind,
        bot_id=r.bot_id,
        label=r.label,
        kinds=tuple(r.kinds or ()),
        key_id=r.key_id,
        nonce=bytes(r.nonce),
        ciphertext=bytes(r.ciphertext),
        auto_use=bool(r.auto_use),
        use_count=int(r.use_count),
        last_used_at=r.last_used_at,
        expires_at=r.expires_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _request(r: Any) -> CredentialRequestRow:
    return CredentialRequestRow(
        id=r.id,
        bot_id=r.bot_id,
        run_id=r.run_id,
        host=r.host,
        page_url=r.page_url,
        purpose=r.purpose,
        fields=list(r.fields or []),
        status=r.status,
        entry_ids=tuple(r.entry_ids or ()),
        saved=bool(r.saved),
        working=dict(r.working or {}),
        created_at=r.created_at,
        decided_at=r.decided_at,
    )


class VaultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- entries -----------------------------------------------------------------

    async def add_entry(
        self,
        entry_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        host: str,
        kind: str,
        bot_id: uuid.UUID | None,
        label: str,
        kinds: list[str],
        key_id: str,
        nonce: bytes,
        ciphertext: bytes,
        expires_at: dt.datetime | None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO vault_entries (id, organization_id, host, kind, bot_id, label,
                                           kinds, key_id, nonce, ciphertext, expires_at)
                VALUES (:id, :org, :host, :kind, :bot, :label, :kinds, :key_id, :nonce,
                        :ct, :exp)
                """
            ),
            {
                "id": entry_id,
                "org": organization_id,
                "host": host,
                "kind": kind,
                "bot": bot_id,
                "label": label,
                "kinds": sorted(kinds),
                "key_id": key_id,
                "nonce": nonce,
                "ct": ciphertext,
                "exp": expires_at,
            },
        )

    async def replace_secret(
        self,
        entry_id: uuid.UUID,
        *,
        label: str,
        kinds: list[str],
        key_id: str,
        nonce: bytes,
        ciphertext: bytes,
    ) -> None:
        """A saved login whose password changed. The row keeps its id, so anything
        pointing at it (a request, an audit line) still points at the right login."""
        await self._s.execute(
            text(
                """
                UPDATE vault_entries
                   SET label = :label, kinds = :kinds, key_id = :key_id, nonce = :nonce,
                       ciphertext = :ct, updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "id": entry_id,
                "label": label,
                "kinds": sorted(kinds),
                "key_id": key_id,
                "nonce": nonce,
                "ct": ciphertext,
            },
        )

    async def get_entry(self, entry_id: uuid.UUID) -> VaultEntryRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_ENTRY_COLUMNS} FROM vault_entries WHERE id = :id"),
                {"id": entry_id},
            )
        ).one_or_none()
        return None if row is None else _entry(row)

    async def options(
        self, organization_id: uuid.UUID, host: str, bot_id: uuid.UUID
    ) -> list[VaultEntryRow]:
        """What could fill a form on `host` for this bot: the organization's saved
        logins for the site, and this bot's unexpired one-time values. Newest first —
        the login a person saved last is the one they meant."""
        rows = (
            await self._s.execute(
                text(
                    f"""
                    SELECT {_ENTRY_COLUMNS} FROM vault_entries
                     WHERE organization_id = :org AND host = :host
                       AND (kind = 'saved' OR (bot_id = :bot AND expires_at > now()))
                     ORDER BY updated_at DESC
                    """
                ),
                {"org": organization_id, "host": host, "bot": bot_id},
            )
        ).all()
        return [_entry(r) for r in rows]

    async def saved(self, organization_id: uuid.UUID) -> list[VaultEntryRow]:
        rows = (
            await self._s.execute(
                text(
                    f"""
                    SELECT {_ENTRY_COLUMNS} FROM vault_entries
                     WHERE organization_id = :org AND kind = 'saved'
                     ORDER BY host, updated_at DESC
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [_entry(r) for r in rows]

    async def mark_used(self, entry_ids: list[uuid.UUID]) -> None:
        if not entry_ids:
            return
        await self._s.execute(
            text(
                "UPDATE vault_entries SET use_count = use_count + 1, last_used_at = now() "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": entry_ids},
        )

    async def set_auto_use(
        self, organization_id: uuid.UUID, entry_id: uuid.UUID, auto_use: bool
    ) -> bool:
        result = await self._s.execute(
            text(
                "UPDATE vault_entries SET auto_use = :auto, updated_at = now() "
                "WHERE id = :id AND organization_id = :org AND kind = 'saved'"
            ),
            {"id": entry_id, "org": organization_id, "auto": auto_use},
        )
        return bool(getattr(result, "rowcount", 0))

    async def delete_entry(self, organization_id: uuid.UUID, entry_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            text("DELETE FROM vault_entries WHERE id = :id AND organization_id = :org"),
            {"id": entry_id, "org": organization_id},
        )
        return bool(getattr(result, "rowcount", 0))

    async def purge_expired(self) -> int:
        """One-time values past their ten minutes. Run on every write to the vault —
        there is no sweeper to forget to schedule, and the table stays small."""
        result = await self._s.execute(
            text("DELETE FROM vault_entries WHERE kind = 'once' AND expires_at <= now()")
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def forget_bot(self, bot_id: uuid.UUID) -> None:
        """A deleted bot's one-time values go with it. Saved logins belong to the
        organization and stay."""
        await self._s.execute(
            text("DELETE FROM vault_entries WHERE bot_id = :bot AND kind = 'once'"),
            {"bot": bot_id},
        )

    # --- requests ----------------------------------------------------------------

    async def add_request(
        self,
        request_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None,
        host: str,
        page_url: str,
        purpose: str,
        fields: list[dict[str, Any]],
        working: dict[str, Any] | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_credential_requests (id, bot_id, run_id, host, page_url,
                                                     purpose, fields, working)
                VALUES (:id, :bot, :run, :host, :url, :purpose, CAST(:fields AS jsonb),
                        CAST(:working AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": request_id,
                "bot": bot_id,
                "run": run_id,
                "host": host,
                "url": page_url[:2_000],
                "purpose": purpose,
                "fields": json.dumps(fields),
                "working": json.dumps(working or {}),
            },
        )

    async def get_request(self, request_id: uuid.UUID) -> CredentialRequestRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_REQUEST_COLUMNS} FROM bot_credential_requests WHERE id = :id"),
                {"id": request_id},
            )
        ).one_or_none()
        return None if row is None else _request(row)

    async def live_requests(self, bot_id: uuid.UUID) -> list[CredentialRequestRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_REQUEST_COLUMNS} FROM bot_credential_requests "
                    "WHERE bot_id = :bot AND status = 'pending' ORDER BY created_at"
                ),
                {"bot": bot_id},
            )
        ).all()
        return [_request(r) for r in rows]

    async def decide_request(
        self,
        request_id: uuid.UUID,
        *,
        status: str,
        entry_ids: list[uuid.UUID] | None = None,
        saved: bool = False,
    ) -> bool:
        """Conditional on still being pending, so a double submit cannot fill twice."""
        result = await self._s.execute(
            text(
                """
                UPDATE bot_credential_requests
                   SET status = :status, entry_ids = :entries, saved = :saved,
                       decided_at = now()
                 WHERE id = :id AND status = 'pending'
                """
            ),
            {
                "id": request_id,
                "status": status,
                "entries": list(entry_ids or []),
                "saved": saved,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def expire_requests(self, bot_id: uuid.UUID) -> None:
        """A new instruction from the person supersedes a form still waiting."""
        await self._s.execute(
            text(
                "UPDATE bot_credential_requests SET status = 'expired', decided_at = now() "
                "WHERE bot_id = :bot AND status = 'pending'"
            ),
            {"bot": bot_id},
        )
