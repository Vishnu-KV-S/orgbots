"""The notifier — the outbox of notifications becomes pushes to the person's devices.

A bot that replies, asks, parks an approval or wants sign-in details writes a row in
`bot_notifications` in the same transaction as the line in its conversation
(`org.bots.BotService`). This loop sends what is unsent to every subscribed device of
that organization (`gateway.push`) and stamps it; a device whose push service says it
is gone is unsubscribed. A notification older than `STALE` is stamped without being
sent — "your bot replied" two days late is noise, not news.

It runs in the worker beside the dispatcher. A runtime with no subscriptions still
stamps its notifications, so the outbox never grows.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass

from runtime.gateway.push import PushSender
from runtime.gateway.vault import VaultUnavailableError
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("runtime.notifier")

TICK_SECONDS = 3.0
STALE = dt.timedelta(hours=12)


@dataclass
class NotifyTick:
    sent: int = 0
    pushed: int = 0
    gone: int = 0


class Notifier:
    def __init__(self, uow_factory: UnitOfWorkFactory, sender: PushSender) -> None:
        self._uow = uow_factory
        self._sender = sender
        self._stopping = asyncio.Event()

    async def tick(self, now: dt.datetime | None = None) -> NotifyTick:
        moment = now or dt.datetime.now(dt.UTC)
        out = NotifyTick()
        async with self._uow() as uow:
            pending = await uow.push.unsent()
        for note in pending:
            async with self._uow.transaction() as uow:
                if not await uow.push.sent(note.id):
                    continue  # another notifier took it
                devices = (
                    []
                    if moment - note.created_at > STALE
                    else await uow.push.subscriptions(note.organization_id)
                )
            out.sent += 1
            payload = {
                "title": note.title,
                "body": note.body,
                "url": note.url,
                "tag": f"bot-{note.bot_id}" if note.bot_id else note.kind,
                "kind": note.kind,
            }
            for device in devices:
                try:
                    outcome = await self._sender.send(device, payload)
                except VaultUnavailableError:
                    log.warning("notifier.no_key")
                    return out
                async with self._uow.transaction() as uow:
                    if outcome == "gone":
                        await uow.push.drop(device.id)
                        out.gone += 1
                    else:
                        await uow.push.delivered(device.id, outcome == "ok")
                        out.pushed += outcome == "ok"
        return out

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("notifier.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)

    def stop(self) -> None:
        self._stopping.set()
