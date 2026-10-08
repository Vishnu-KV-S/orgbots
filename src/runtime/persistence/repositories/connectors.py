"""Connectors (MCP servers) per organization. See migration 049.

Stores a connector's token only as ciphertext and holds no key, like the vault's
repository: the API seals, the gateway opens.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = """
    id, organization_id, name, title, url, auth_kind, header_name, secret_key_id,
    secret_nonce, secret_ciphertext, tools, server_name, status, last_error, enabled,
    catalog_key, created_at, updated_at
"""

EDITABLE = frozenset(
    {
        "title",
        "url",
        "auth_kind",
        "header_name",
        "secret_key_id",
        "secret_nonce",
        "secret_ciphertext",
        "tools",
        "server_name",
        "status",
        "last_error",
        "enabled",
    }
)


@dataclass(frozen=True, slots=True)
class ConnectorRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    title: str
    url: str
    auth_kind: str
    header_name: str
    secret_key_id: str | None
    secret_nonce: bytes | None
    secret_ciphertext: bytes | None
    tools: list[dict[str, Any]]
    server_name: str
    status: str
    last_error: str
    enabled: bool
    catalog_key: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def has_secret(self) -> bool:
        return self.secret_ciphertext is not None

    def __repr__(self) -> str:
        return f"ConnectorRow(id={self.id}, name={self.name!r})"


def _row(r: Any) -> ConnectorRow:
    return ConnectorRow(
        id=r.id,
        organization_id=r.organization_id,
        name=r.name,
        title=r.title,
        url=r.url,
        auth_kind=r.auth_kind,
        header_name=r.header_name,
        secret_key_id=r.secret_key_id,
        secret_nonce=bytes(r.secret_nonce) if r.secret_nonce is not None else None,
        secret_ciphertext=bytes(r.secret_ciphertext) if r.secret_ciphertext is not None else None,
        tools=list(r.tools or []),
        server_name=r.server_name,
        status=r.status,
        last_error=r.last_error,
        enabled=r.enabled,
        catalog_key=r.catalog_key,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


class ConnectorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        connector_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        name: str,
        title: str,
        url: str,
        auth_kind: str,
        header_name: str,
        catalog_key: str | None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO bot_connectors (id, organization_id, name, title, url, auth_kind,
                                            header_name, catalog_key)
                VALUES (:id, :org, :name, :title, :url, :auth, :header, :catalog)
                """
            ),
            {
                "id": connector_id,
                "org": organization_id,
                "name": name,
                "title": title,
                "url": url,
                "auth": auth_kind,
                "header": header_name,
                "catalog": catalog_key,
            },
        )

    async def update(self, connector_id: uuid.UUID, fields: dict[str, Any]) -> None:
        unknown = set(fields) - EDITABLE
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(
            f"{k} = CAST(:{k} AS jsonb)" if k == "tools" else f"{k} = :{k}" for k in sorted(fields)
        )
        params = {k: json.dumps(v) if k == "tools" else v for k, v in fields.items()}
        await self._s.execute(
            text(f"UPDATE bot_connectors SET {sets}, updated_at = now() WHERE id = :id"),
            {**params, "id": connector_id},
        )

    async def get(self, connector_id: uuid.UUID) -> ConnectorRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_connectors WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": connector_id},
            )
        ).first()
        return None if row is None else _row(row)

    async def by_name(self, organization_id: uuid.UUID, name: str) -> ConnectorRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_connectors WHERE organization_id = :org "
                    "AND name = :name AND deleted_at IS NULL"
                ),
                {"org": organization_id, "name": name.strip().lower()},
            )
        ).first()
        return None if row is None else _row(row)

    async def for_organization(self, organization_id: uuid.UUID) -> list[ConnectorRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_connectors WHERE organization_id = :org "
                    "AND deleted_at IS NULL ORDER BY name"
                ),
                {"org": organization_id},
            )
        ).all()
        return [_row(r) for r in rows]

    async def soft_delete(self, connector_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bot_connectors SET deleted_at = now() WHERE id = :id"),
            {"id": connector_id},
        )
