"""Push subscriptions, the notification outbox and the VAPID key. See migration 050."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SubscriptionRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    endpoint: str
    p256dh: str
    auth: str
    failures: int


@dataclass(frozen=True, slots=True)
class NotificationRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    bot_id: uuid.UUID | None
    kind: str
    title: str
    body: str
    url: str
    created_at: dt.datetime
    member_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PushKeyRow:
    public_key: str
    key_id: str
    nonce: bytes
    ciphertext: bytes


class PushRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- subscriptions -------------------------------------------------------------------

    async def subscribe(
        self,
        organization_id: uuid.UUID,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str,
        member_id: uuid.UUID | None = None,
    ) -> None:
        """A device subscribing again (another member signed in on it) becomes theirs."""
        await self._s.execute(
            text(
                """
                INSERT INTO push_subscriptions (id, organization_id, endpoint, p256dh, auth,
                                                user_agent, member_id)
                VALUES (:id, :org, :endpoint, :p256dh, :auth, :ua, :member)
                ON CONFLICT (endpoint) DO UPDATE
                   SET organization_id = EXCLUDED.organization_id, p256dh = EXCLUDED.p256dh,
                       auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent, failures = 0,
                       member_id = EXCLUDED.member_id
                """
            ),
            {
                "id": uuid.uuid4(),
                "member": member_id,
                "org": organization_id,
                "endpoint": endpoint,
                "p256dh": p256dh,
                "auth": auth,
                "ua": user_agent[:300],
            },
        )

    async def unsubscribe(self, organization_id: uuid.UUID, endpoint: str) -> None:
        await self._s.execute(
            text("DELETE FROM push_subscriptions WHERE organization_id = :org AND endpoint = :e"),
            {"org": organization_id, "e": endpoint},
        )

    async def subscriptions(
        self, organization_id: uuid.UUID, member_id: uuid.UUID | None = None
    ) -> list[SubscriptionRow]:
        """The organization's devices — or, with `member_id`, only that member's."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, organization_id, endpoint, p256dh, auth, failures "
                    "FROM push_subscriptions WHERE organization_id = :org "
                    "AND (CAST(:member AS uuid) IS NULL OR member_id = :member)"
                ),
                {"org": organization_id, "member": member_id},
            )
        ).all()
        return [
            SubscriptionRow(r.id, r.organization_id, r.endpoint, r.p256dh, r.auth, r.failures)
            for r in rows
        ]

    async def delivered(self, subscription_id: uuid.UUID, ok: bool) -> None:
        await self._s.execute(
            text(
                "UPDATE push_subscriptions SET "
                "failures = CASE WHEN :ok THEN 0 ELSE failures + 1 END, "
                "last_ok_at = CASE WHEN :ok THEN now() ELSE last_ok_at END WHERE id = :id"
            ),
            {"id": subscription_id, "ok": ok},
        )

    async def drop(self, subscription_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM push_subscriptions WHERE id = :id"), {"id": subscription_id}
        )

    # --- the outbox ------------------------------------------------------------------------

    async def notify(
        self,
        notification_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        bot_id: uuid.UUID | None,
        kind: str,
        title: str,
        body: str,
        url: str,
        member_id: uuid.UUID | None = None,
    ) -> None:
        """`member_id` is whose devices hear it; None is every device in the organization
        (a runtime without members)."""
        await self._s.execute(
            text(
                """
                INSERT INTO bot_notifications (id, organization_id, bot_id, kind, title, body,
                                               url, member_id)
                VALUES (:id, :org, :bot, :kind, :title, :body, :url, :member)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": notification_id,
                "org": organization_id,
                "member": member_id,
                "bot": bot_id,
                "kind": kind,
                "title": title[:120],
                "body": body[:300],
                "url": url[:500],
            },
        )

    async def unsent(self, limit: int = 100) -> list[NotificationRow]:
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, organization_id, bot_id, kind, title, body, url, created_at, "
                    "member_id FROM bot_notifications WHERE sent_at IS NULL "
                    "ORDER BY created_at LIMIT :n"
                ),
                {"n": limit},
            )
        ).all()
        return [NotificationRow(**r._mapping) for r in rows]

    async def sent(self, notification_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            text("UPDATE bot_notifications SET sent_at = now() WHERE id = :id AND sent_at IS NULL"),
            {"id": notification_id},
        )
        return bool(getattr(result, "rowcount", 0))

    # --- the VAPID key -----------------------------------------------------------------------

    async def key(self) -> PushKeyRow | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT public_key, key_id, nonce, ciphertext FROM push_keys "
                    "WHERE name = 'vapid'"
                )
            )
        ).first()
        if row is None:
            return None
        return PushKeyRow(row.public_key, row.key_id, bytes(row.nonce), bytes(row.ciphertext))

    async def save_key(self, public_key: str, key_id: str, nonce: bytes, ciphertext: bytes) -> bool:
        """False when another process made the key first; read that one instead."""
        result = await self._s.execute(
            text(
                "INSERT INTO push_keys (name, public_key, key_id, nonce, ciphertext) "
                "VALUES ('vapid', :pub, :kid, :nonce, :ct) ON CONFLICT (name) DO NOTHING"
            ),
            {"pub": public_key, "kid": key_id, "nonce": nonce, "ct": ciphertext},
        )
        return bool(getattr(result, "rowcount", 0))
