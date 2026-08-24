"""Rate limit policies.

Policy in Postgres, counters in Redis. This half is the durable one, and it is read
through a TTL cache in the gateway for the same reason permissions are: a policy read
per tool call would be a round trip the §9 latency budget notices, and a policy that
takes thirty seconds to take effect is fine when the thing it protects is a
per-minute quota.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    scope_type: str
    scope_id: str
    limit_per_window: int
    window_seconds: int
    burst: int
    fail_open: bool

    @property
    def key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"

    @property
    def refill_per_second(self) -> float:
        return self.limit_per_window / self.window_seconds


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def policies(self, organization_id: uuid.UUID) -> list[RateLimitPolicy]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT scope_type, scope_id, limit_per_window, window_seconds,
                           burst, fail_open
                      FROM rate_limit_policies
                     WHERE organization_id = :org AND enabled
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            RateLimitPolicy(
                scope_type=r.scope_type,
                scope_id=r.scope_id,
                limit_per_window=int(r.limit_per_window),
                window_seconds=int(r.window_seconds),
                burst=int(r.burst),
                fail_open=bool(r.fail_open),
            )
            for r in rows
        ]

    async def upsert(
        self,
        policy_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        scope_type: str,
        scope_id: str,
        limit_per_window: int,
        window_seconds: int,
        burst: int | None = None,
        fail_open: bool = True,
        enabled: bool = True,
    ) -> None:
        """`burst` defaults to the window limit — a bucket that holds exactly one
        window's worth. A smaller burst would make the policy quietly deliver less
        than its stated limit, which is why 021 has a CHECK against it."""
        await self._s.execute(
            text(
                """
                INSERT INTO rate_limit_policies (id, organization_id, scope_type, scope_id,
                                                 limit_per_window, window_seconds, burst,
                                                 fail_open, enabled)
                VALUES (:id, :org, :st, :sid, :limit, :window, :burst, :fo, :en)
                ON CONFLICT ON CONSTRAINT uq_rate_limit_scope DO UPDATE
                    SET limit_per_window = EXCLUDED.limit_per_window,
                        window_seconds = EXCLUDED.window_seconds,
                        burst = EXCLUDED.burst,
                        fail_open = EXCLUDED.fail_open,
                        enabled = EXCLUDED.enabled
                """
            ),
            {
                "id": policy_id,
                "org": organization_id,
                "st": scope_type,
                "sid": scope_id,
                "limit": limit_per_window,
                "window": window_seconds,
                "burst": burst if burst is not None else limit_per_window,
                "fo": fail_open,
                "en": enabled,
            },
        )
