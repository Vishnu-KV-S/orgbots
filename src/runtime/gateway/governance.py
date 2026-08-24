"""The parts of the governance pipeline that exist to make it affordable.

M2 §1 lists what governance adds. M2 §9 requires that adding it costs less than 10%
of the cost per accepted outcome and 25% of P95 latency, and §10 names the three
things that blow that budget: *audit writes not batched, permission checks not
cached, reservation round-trips added per call instead of per node*. Two of those
three are this module.

**`AuditBuffer`** collects decision rows during one gateway call and writes them in
one statement. A tool call passes eight checks; eight `INSERT`s would be eight round
trips on a path that previously had three. Buffered, it is one — and, where the
gateway is already opening a transaction for the budget reconcile, zero extra.

**`PermissionCache`** answers "does this actor still hold a grant for this tool" from
memory for up to 30 seconds. The bound is edge case 64's, and it is a *maximum* rather
than a target: revocation must take effect inside a running run, and 30s is the number
that was specified. The cache is keyed per actor rather than per call, so a run making
twenty tool calls does one query.

Both are deliberately dumb — no eviction policy beyond TTL, no size cap, no metrics of
their own. A cache with its own behaviour is a second system to reason about, and the
thing being cached here is four rows that change by the week.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from runtime.domain.enums import AuditSeverity, GatewayDecision
from runtime.domain.ids import OrganizationId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.audit import DecisionRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

log = get_logger("gateway.governance")

PERMISSION_CACHE_TTL_SECONDS = 30.0
"""Edge case 64: a revoked grant must be caught by the next gateway call within this
window. Not a tuning knob — raising it widens how long a revoked actor keeps working,
which is the exact thing the check exists to bound."""

MAX_BUFFERED_DECISIONS = 64
"""Flush early rather than grow without bound. A single gateway call writes fewer than
ten rows; reaching this means something is looping, and buffering a loop's audit trail
in memory is how an incident becomes an OOM."""


@dataclass
class AuditBuffer:
    """Decision rows for one gateway call, written once.

    Not thread-safe and not shared: one buffer per `execute()`. A buffer shared across
    concurrent calls would interleave two runs' decisions into one flush, and a partial
    flush would then attribute rows to whichever call happened to fail.
    """

    rows: list[DecisionRow] = field(default_factory=list)

    def add(
        self,
        *,
        organization_id: OrganizationId,
        gateway: str,
        subject: str,
        decision: GatewayDecision,
        check_name: str,
        reason: str | None = None,
        severity: AuditSeverity = AuditSeverity.NORMAL,
        **rest: object,
    ) -> None:
        self.rows.append(
            DecisionRow(
                organization_id=organization_id,
                gateway=gateway,
                subject=subject,
                decision=decision,
                check_name=check_name,
                reason=reason,
                severity=severity,
                **rest,  # type: ignore[arg-type]
            )
        )

    @property
    def full(self) -> bool:
        return len(self.rows) >= MAX_BUFFERED_DECISIONS

    async def flush_into(self, uow: UnitOfWork) -> int:
        """Write and clear, inside the caller's transaction.

        Taking a `UnitOfWork` rather than the factory is what lets the gateway fold
        the audit write into a transaction it was opening anyway — which is the
        difference between one round trip and two on the common path.
        """
        if not self.rows:
            return 0
        written = await uow.audit.record_decisions(self.rows)
        self.rows.clear()
        return written

    async def flush(self, uow_factory: UnitOfWorkFactory) -> int:
        """Write in a transaction of our own. Used on the refusal paths, where there
        is no other transaction to join — and a denial is the row we least want to
        lose, since the denial stream is the §9 deliverable."""
        if not self.rows:
            return 0
        async with uow_factory.transaction() as uow:
            return await self.flush_into(uow)


@dataclass(frozen=True, slots=True)
class _Grants:
    tools: frozenset[str]
    expires_at: float


class PermissionCache:
    """Live tool grants per actor, cached for at most 30 seconds."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        ttl_seconds: float = PERMISSION_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow_factory
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], _Grants] = {}

    async def live_tools(
        self, organization_id: OrganizationId, actor_name: str, role_name: str | None
    ) -> frozenset[str]:
        key = (str(organization_id), actor_name)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit.expires_at > now:
            return hit.tools
        async with self._uow() as uow:
            tools = await uow.authority.live_tools(organization_id, actor_name, role_name)
        self._cache[key] = _Grants(tools, now + self._ttl)
        return tools

    async def holds(
        self, organization_id: OrganizationId, actor_name: str, role_name: str | None, tool: str
    ) -> bool:
        return tool in await self.live_tools(organization_id, actor_name, role_name)

    def invalidate(self, organization_id: OrganizationId | None = None) -> None:
        """Called by the operator CLI after a revoke, so "revoked" means revoked now
        rather than revoked in up to thirty seconds. The TTL is the guarantee; this is
        the courtesy."""
        if organization_id is None:
            self._cache.clear()
            return
        wanted = str(organization_id)
        for key in [k for k in self._cache if k[0] == wanted]:
            del self._cache[key]
