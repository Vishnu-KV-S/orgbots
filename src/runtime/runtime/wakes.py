"""The wake runner — queued deliveries become bot turns when their bot is free.

A message from one bot to another, a group message that named a bot, and a check-in a
bot set itself (`check_back`, held until its `due_at`) are queued (`bot_wakes`) rather
than started: a bot running two turns at once would fight itself
for its screen, and a teammate's message must not supersede the work the person gave
it. So the oldest delivery of each free bot starts (`BotManager.start_wake`), busy
bots keep theirs — "free" is `BotManager.busy`, the same test a routine waits on — and
a delivery that has waited `MAX_WAIT_HOURS` is dropped, with the reason on the row.

A delivery is claimed with a conditional update before its run starts, so of two
runners exactly one starts it; the run is idempotent on the delivery's id as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field

from runtime.domain.groups import MAX_WAIT_HOURS
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.groups import WakeRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bots import BotManager, BotNotFoundError

log = get_logger("runtime.wakes")

TICK_SECONDS = 5.0
"""Bots waiting on each other feel slow at the routine runner's 20 seconds."""


@dataclass
class WakeTick:
    started: int = 0
    skipped: int = 0
    waiting: list[str] = field(default_factory=list)


class WakeRunner:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, bots: BotManager, *, interval_s: float = TICK_SECONDS
    ) -> None:
        self._uow = uow_factory
        self._bots = bots
        self._interval = interval_s
        self._stopping = asyncio.Event()

    async def tick(self, now: dt.datetime | None = None) -> WakeTick:
        moment = now or dt.datetime.now(dt.UTC)
        out = WakeTick()
        async with self._uow() as uow:
            queued = await uow.groups.queued_wakes()
        seen: set[object] = set()
        for wake in queued:
            if wake.bot_id in seen:
                continue
            seen.add(wake.bot_id)
            await self._start_one(wake, moment, out)
        return out

    async def _skip(self, wake: WakeRow, reason: str, out: WakeTick) -> None:
        async with self._uow.transaction() as uow:
            if await uow.groups.claim_wake(wake.id):
                await uow.groups.finish_wake(wake.id, status="skipped", run_id=None, detail=reason)
                out.skipped += 1

    async def _start_one(self, wake: WakeRow, now: dt.datetime, out: WakeTick) -> None:
        try:
            bot = await self._bots.get(wake.bot_id)
        except BotNotFoundError:
            await self._skip(wake, "the bot was deleted", out)
            return
        reason = await self._bots.busy(bot)
        if reason is not None:
            # A check-in has waited only since it fell due, not since it was set.
            if now - (wake.due_at or wake.created_at) > dt.timedelta(hours=MAX_WAIT_HOURS):
                await self._skip(wake, f"{bot.name} was {reason} for {MAX_WAIT_HOURS} hours", out)
            else:
                out.waiting.append(f"{bot.name}: {reason}")
            return
        async with self._uow.transaction() as uow:
            if not await uow.groups.claim_wake(wake.id):
                return
        try:
            sent = await self._bots.start_wake(wake)
        except Exception as exc:
            log.exception("wake.start_failed", wake_id=str(wake.id))
            async with self._uow.transaction() as uow:
                await uow.groups.finish_wake(
                    wake.id, status="skipped", run_id=None, detail=f"{type(exc).__name__}: {exc}"
                )
            out.skipped += 1
            return
        async with self._uow.transaction() as uow:
            await uow.groups.finish_wake(
                wake.id,
                status="started" if sent.admitted else "skipped",
                run_id=sent.run_id,
                detail=sent.refusal_reason or "",
            )
        if sent.admitted:
            out.started += 1

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("wake.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)

    def stop(self) -> None:
        self._stopping.set()
