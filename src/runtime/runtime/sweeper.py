"""The governance sweeper.

Three timed jobs that nothing was running.

`Settings` has carried `budget_sweep_interval_seconds` and
`approval_sweep_interval_seconds` since M0 and M1 respectively, and until now the only
things that called the sweeps were the CLI's `tick` and the tests. That was survivable
in M1, where the one time-driven behaviour was a 24-hour TTL nobody was waiting on.
M2 makes it load-bearing:

*Approvals escalate on a clock.* An escalation chain that is never walked is a list.
Everything §5 describes — escalate, then apply `on_expiry` at `max_escalations` —
happens here or not at all.

*Reservations expire on a clock.* §4: *"Reservation sweeper runs every 30s against
`expires_at`."* Without it, one dead worker holds headroom at every level of the chain
until the TTL, and with a hierarchy that is the whole organization's headroom rather
than one actor's.

*Audit partitions are created ahead of the window.* Cheap, monthly, and the
alternative is rows landing in the DEFAULT partition where a `DROP TABLE` cannot
reach them.

Each job runs on its own interval and catches its own exceptions. A sweeper that died
on one bad row and took the other two jobs with it would be worse than no sweeper,
because the intervals would still be configured and somebody would believe they ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

from runtime.budget.service import BudgetService
from runtime.observability.logging import get_logger
from runtime.org.approvals import ApprovalService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("runtime.sweeper")

PARTITION_SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
"""Four times a day. The job creates next month's audit partition, which needs doing
once a month — the frequency is about surviving a process that was down on the 1st,
not about the work."""

PARTITION_MONTHS_AHEAD = 3


class GovernanceSweeper:
    """Runs the timed halves of §4 and §5. Start it once per deployment."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        settings: Settings | None = None,
        budget: BudgetService | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        self._approvals = ApprovalService(uow_factory)
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        await asyncio.gather(
            self._loop(self._settings.budget_sweep_interval_seconds, self.sweep_budget),
            self._loop(self._settings.approval_sweep_interval_seconds, self.sweep_approvals),
            self._loop(PARTITION_SWEEP_INTERVAL_SECONDS, self.ensure_partitions),
        )

    async def _loop(self, interval: float, job) -> None:  # type: ignore[no-untyped-def]
        while not self._stopping.is_set():
            try:
                await job()
            except Exception as exc:
                log.error("sweeper.failed", job=job.__name__, error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)

    async def sweep_budget(self) -> int:
        """Reclaim holds whose worker died before reconciling (§4, edge case 20)."""
        async with self._uow.transaction() as uow:
            reclaimed = await self._budget.sweep(uow)
        if reclaimed:
            log.warning("sweeper.reservations_reclaimed", count=reclaimed)
        return reclaimed

    async def sweep_approvals(self) -> tuple[int, int]:
        """Escalate what can escalate, then settle what cannot (§5).

        The order is `ApprovalService.sweep`'s, not this method's — an approval with
        chain left has been ignored, not expired, and those want different responses.
        """
        escalated, expired = await self._approvals.sweep()
        if escalated or expired:
            log.info("sweeper.approvals", escalated=len(escalated), expired=len(expired))
        return len(escalated), len(expired)

    async def ensure_partitions(self, now: dt.datetime | None = None) -> list[str]:
        """Create the audit partitions for the next few months."""
        moment = now or dt.datetime.now(dt.UTC)
        created: list[str] = []
        async with self._uow.transaction() as uow:
            for offset in range(PARTITION_MONTHS_AHEAD + 1):
                month = (
                    (moment.replace(day=1) + dt.timedelta(days=32 * offset)).date().replace(day=1)
                )
                created.append(await uow.audit.ensure_partition(month))
        return created
