"""Encrypted credentials.

The repository stores and returns *ciphertext*. It cannot decrypt and does not hold
a key — that is `runtime.gateway.credentials.CredentialCipher`, one layer up, and the
separation is what keeps the plaintext out of the persistence layer entirely. A bug
here leaks nothing that a database dump would not already have.

`rotate()` is one statement plus one insert inside the caller's transaction, and the
partial unique index `uq_credential_active` is what makes it safe: two concurrent
rotations cannot both leave an ACTIVE row, so the loser's transaction aborts rather
than half the fleet using each version.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import CredentialStatus


@dataclass(frozen=True, slots=True)
class CredentialRow:
    id: uuid.UUID
    name: str
    provider: str
    version: int
    status: CredentialStatus
    key_id: str
    nonce: bytes
    ciphertext: bytes
    fingerprint: str
    expires_at: dt.datetime | None
    rotated_at: dt.datetime | None


class CredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def active(self, organization_id: uuid.UUID, name: str) -> CredentialRow | None:
        """The credential the gateway should use *right now*.

        Fetched per call, never pinned onto a run. That is what makes T37 hold: a
        rotation lands, the next call reads this row, and nothing in flight is holding
        the old plaintext.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, name, provider, version, status, key_id, nonce, ciphertext,
                           fingerprint, expires_at, rotated_at
                      FROM credentials
                     WHERE organization_id = :org AND name = :name AND status = 'ACTIVE'
                    """
                ),
                {"org": organization_id, "name": name},
            )
        ).one_or_none()
        return _row(row) if row is not None else None

    async def put(
        self,
        credential_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        name: str,
        provider: str,
        version: int,
        key_id: str,
        nonce: bytes,
        ciphertext: bytes,
        fingerprint: str,
        expires_at: dt.datetime | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO credentials (id, organization_id, name, provider, version,
                                         status, key_id, nonce, ciphertext, fingerprint,
                                         expires_at)
                VALUES (:id, :org, :name, :provider, :version, 'ACTIVE', :key_id,
                        :nonce, :ct, :fp, :exp)
                """
            ),
            {
                "id": credential_id,
                "org": organization_id,
                "name": name,
                "provider": provider,
                "version": version,
                "key_id": key_id,
                "nonce": nonce,
                "ct": ciphertext,
                "fp": fingerprint,
                "exp": expires_at,
            },
        )

    async def retire(self, organization_id: uuid.UUID, name: str, *, by: str) -> int:
        """Retire whatever is currently active. Returns rows affected.

        Called immediately before `put()` inserts the new version, inside one
        transaction. The order matters: retiring first means the partial unique index
        never sees two ACTIVE rows even momentarily, so a concurrent rotation blocks
        on the row lock rather than failing on the index.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE credentials
                       SET status = 'RETIRED', rotated_at = now(), rotated_by = :by
                     WHERE organization_id = :org AND name = :name AND status = 'ACTIVE'
                    RETURNING id
                    """
                ),
                {"org": organization_id, "name": name, "by": by},
            )
        ).all()
        return len(rows)

    async def next_version(self, organization_id: uuid.UUID, name: str) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT COALESCE(max(version), 0) + 1 FROM credentials "
                        "WHERE organization_id = :org AND name = :name"
                    ),
                    {"org": organization_id, "name": name},
                )
            ).scalar_one()
        )

    async def touch(self, credential_id: uuid.UUID) -> None:
        """Stamp `last_fetched_at`. Deliberately not called from the hot path — see
        the note in migration 022. The operator CLI calls it when asked "is this
        still in use"."""
        await self._s.execute(
            text("UPDATE credentials SET last_fetched_at = now() WHERE id = :id"),
            {"id": credential_id},
        )

    async def list_for_org(self, organization_id: uuid.UUID) -> list[CredentialRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, name, provider, version, status, key_id, nonce, ciphertext,
                           fingerprint, expires_at, rotated_at
                      FROM credentials WHERE organization_id = :org
                     ORDER BY name, version DESC
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [_row(r) for r in rows]


def _row(r: object) -> CredentialRow:
    return CredentialRow(
        id=r.id,  # type: ignore[attr-defined]
        name=r.name,  # type: ignore[attr-defined]
        provider=r.provider,  # type: ignore[attr-defined]
        version=int(r.version),  # type: ignore[attr-defined]
        status=CredentialStatus(r.status),  # type: ignore[attr-defined]
        key_id=r.key_id,  # type: ignore[attr-defined]
        nonce=bytes(r.nonce),  # type: ignore[attr-defined]
        ciphertext=bytes(r.ciphertext),  # type: ignore[attr-defined]
        fingerprint=r.fingerprint,  # type: ignore[attr-defined]
        expires_at=r.expires_at,  # type: ignore[attr-defined]
        rotated_at=r.rotated_at,  # type: ignore[attr-defined]
    )
