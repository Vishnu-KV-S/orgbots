"""Web Push — telling the person's devices that a bot needs them.

A browser that subscribed (the installed app, or any browser tab where the person
turned push on) gave the runtime an endpoint at its push service and two keys; a push
is a small JSON payload encrypted to those keys and signed with the server's VAPID key,
POSTed to the endpoint. `pywebpush` does the encryption and the signing; this module
owns the key and the outcome.

**The VAPID key is made once, on first use**, and kept in `push_keys` with its private
half sealed by the credential cipher — the same rule as every other secret here: in the
database only as ciphertext, opened in the gateway, never logged. Its public half is
what a browser subscribes with, so it must not change: a new key would silently orphan
every subscription.

A push service that says a subscription is gone (404, 410) gets it deleted; anything
else counts a failure and leaves it for next time. Sending is a blocking HTTP call, so
it runs in a thread.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from runtime.gateway.vault import load_cipher
from runtime.persistence.repositories.push import SubscriptionRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

_AAD = b"push:vapid"
TTL_S = 6 * 3600
"""How long a push service holds a notification for a device that is off."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class PushSender:
    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings) -> None:
        self._uow = uow_factory
        self._settings = settings
        self._private: str | None = None
        self._public: str | None = None

    async def public_key(self) -> str:
        """The VAPID public key a browser subscribes with (base64url, uncompressed)."""
        await self._load()
        assert self._public is not None
        return self._public

    async def _load(self) -> None:
        if self._private is not None:
            return
        cipher = load_cipher(self._settings)
        async with self._uow() as uow:
            row = await uow.push.key()
        if row is None:
            key = ec.generate_private_key(ec.SECP256R1())
            private = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
            public = _b64url(
                key.public_key().public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
                )
            )
            key_id, nonce, ciphertext = cipher.encrypt(private, aad=_AAD)
            async with self._uow.transaction() as uow:
                if not await uow.push.save_key(public, key_id, nonce, ciphertext):
                    row = await uow.push.key()
            if row is None:
                self._private, self._public = private, public
                return
        assert row is not None
        self._private = cipher.decrypt(row.key_id, row.nonce, row.ciphertext, aad=_AAD)
        self._public = row.public_key

    async def send(self, subscription: SubscriptionRow, payload: dict[str, Any]) -> str:
        """`ok`, `gone` (the subscription should be deleted) or `failed`."""
        await self._load()
        from pywebpush import WebPushException, webpush

        info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }

        def deliver() -> str:
            try:
                webpush(
                    subscription_info=info,
                    data=json.dumps(payload),
                    vapid_private_key=self._private,
                    vapid_claims={"sub": self._settings.push_contact},
                    ttl=TTL_S,
                    timeout=15,
                )
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                return "gone" if status in (404, 410) else "failed"
            except Exception:
                return "failed"
            return "ok"

        return await asyncio.to_thread(deliver)
