"""The kill switch.

M0's was a dataclass on the gateway — a process-local boolean an operator could only
change by redeploying. M2's is a table, a 10s cache, and two modes.

**`drain` is the default and `halt` is the exception.** The difference is one check:

    drain   refuses new runs and new tool calls; a call already past the journal
            completes and commits. Zero orphan INTENT rows. T40.
    halt    also refuses *after* the effect has fired, so the journal keeps an INTENT
            row and the effect is left for a human to reconcile. T41.

`halt` is the right choice when an effect in flight is worse than an effect you have
to reconcile — a runaway publish loop, a compromised credential. It is the wrong
default, because most incidents are not that, and `halt` turns every in-flight call
into manual work at the exact moment nobody has the attention for it.

**The 10s cache** (edge case 70) is the whole reason this is usable on a hot path. An
uncached read is a query per gateway call; a 10s cache means engaging a switch takes
effect while the operator is still looking at the screen, and costs one query per
organization per ten seconds. Both halves of that matter: a five-minute cache would
make the kill switch a thing you engage and then wonder about.

The cache is *negative-cached too* — "no switches" is a result worth remembering, and
it is the answer 99.99% of the time.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from runtime.domain.enums import AuditSeverity, KillMode, KillScope
from runtime.domain.errors import KillSwitchEngaged
from runtime.domain.ids import OrganizationId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.killswitch import KillSwitchRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("org.killswitch")

KILL_CACHE_TTL_SECONDS = 10.0
"""§7: fast enough to matter, slow enough not to hammer Postgres."""


@dataclass(frozen=True, slots=True)
class KillVerdict:
    """What the switches say about one attempted call."""

    stopped: bool
    mode: KillMode | None = None
    reason: str = ""
    scope: str = ""

    def raise_if_stopped(self, what: str) -> None:
        if self.stopped:
            raise KillSwitchEngaged(
                f"kill switch ({self.mode.value if self.mode else '?'}, scope {self.scope}) "
                f"stops {what}: {self.reason}"
            )


ALLOWED = KillVerdict(stopped=False)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    switches: tuple[KillSwitchRow, ...]
    expires_at: float


class KillSwitchService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        ttl_seconds: float = KILL_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow = uow_factory
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    async def switches(self, organization_id: OrganizationId) -> tuple[KillSwitchRow, ...]:
        key = str(organization_id)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit.expires_at > now:
            return hit.switches
        async with self._uow() as uow:
            rows = tuple(await uow.killswitch.active(organization_id))
        self._cache[key] = _CacheEntry(rows, now + self._ttl)
        return rows

    async def check(
        self,
        organization_id: OrganizationId,
        *,
        tool: str | None = None,
        actor: str | None = None,
        connection: str | None = None,
        post_effect: bool = False,
    ) -> KillVerdict:
        """Does anything stop this call?

        `post_effect=True` is the check made *after* an effect has fired, and it is
        the only place the two modes differ. A `drain` switch answers no there — the
        effect happened, committing the record of it is the only way to avoid an
        orphan — while a `halt` switch answers yes and leaves the INTENT row standing.

        Ordering matters when several switches match: `halt` wins over `drain`, so an
        operator escalating from drain to halt does not have to disengage the first
        one under pressure.
        """
        matches = [
            s
            for s in await self.switches(organization_id)
            if s.covers(tool=tool, actor=actor, connection=connection)
        ]
        if not matches:
            return ALLOWED
        matches.sort(key=lambda s: 0 if s.mode is KillMode.HALT else 1)
        switch = matches[0]
        if post_effect and switch.mode is not KillMode.HALT:
            return ALLOWED
        return KillVerdict(
            stopped=True,
            mode=switch.mode,
            reason=switch.reason,
            scope=f"{switch.scope_type.value}:{switch.scope_id or '*'}",
        )

    async def admits_runs(self, organization_id: OrganizationId, actor: str) -> KillVerdict:
        """Admission check. Both modes refuse new runs — that is what `drain` means."""
        return await self.check(organization_id, actor=actor)

    # --- operator actions ------------------------------------------------------------

    async def engage(
        self,
        organization_id: OrganizationId,
        *,
        scope_type: KillScope,
        scope_id: str | None,
        mode: KillMode = KillMode.DRAIN,
        reason: str,
        engaged_by: str = "operator",
    ) -> bool:
        async with self._uow.transaction() as uow:
            switch_id = await uow.killswitch.engage(
                uuid.uuid4(),
                organization_id,
                scope_type=scope_type,
                scope_id=scope_id,
                mode=mode,
                reason=reason,
                engaged_by=engaged_by,
            )
            if switch_id is not None:
                await uow.audit.record(
                    organization_id=organization_id,
                    action="killswitch.engaged",
                    target=f"{scope_type.value}:{scope_id or '*'}",
                    severity=AuditSeverity.HIGH,
                    outcome=mode.value,
                    detail={"reason": reason, "engaged_by": engaged_by},
                )
        self.invalidate(organization_id)
        if switch_id is None:
            return False
        log.warning(
            "killswitch.engaged",
            scope=f"{scope_type.value}:{scope_id or '*'}",
            mode=mode.value,
            reason=reason,
            engaged_by=engaged_by,
        )
        return True

    async def disengage(
        self,
        organization_id: OrganizationId,
        *,
        scope_type: KillScope,
        scope_id: str | None,
        disengaged_by: str = "operator",
    ) -> bool:
        async with self._uow.transaction() as uow:
            ok = await uow.killswitch.disengage(
                organization_id,
                scope_type=scope_type,
                scope_id=scope_id,
                disengaged_by=disengaged_by,
            )
        self.invalidate(organization_id)
        if ok:
            log.warning(
                "killswitch.disengaged",
                scope=f"{scope_type.value}:{scope_id or '*'}",
                by=disengaged_by,
            )
        return ok

    def invalidate(self, organization_id: OrganizationId | None = None) -> None:
        """Drop the cache. Called after every engage/disengage so the operator who
        just pulled the switch does not spend ten seconds wondering whether it took."""
        if organization_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(organization_id), None)
