"""Connectors, as the graph sees them: which apps a bot can call, and their tools.

Read-only. Connecting, refreshing and sealing a token are the API's (`api.connectors`),
because they make a network call a run must not make outside the gateway; a run only
reads the list the API stored, and calls a tool through `connector.call@1`.
"""

from __future__ import annotations

import uuid
from typing import Any

from runtime.persistence.repositories.connectors import ConnectorRow
from runtime.persistence.uow import UnitOfWorkFactory


class ConnectorService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def usable(self, organization_id: uuid.UUID) -> list[ConnectorRow]:
        """Connectors a bot may call: enabled, and connected last time it was tried."""
        async with self._uow() as uow:
            rows = await uow.connectors.for_organization(organization_id)
        return [r for r in rows if r.enabled and r.status == "ok"]

    async def for_prompt(
        self, organization_id: uuid.UUID
    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
        return [(r.name, r.title, r.tools) for r in await self.usable(organization_id)]

    async def by_name(self, organization_id: uuid.UUID, name: str) -> ConnectorRow | None:
        async with self._uow() as uow:
            return await uow.connectors.by_name(organization_id, name)
