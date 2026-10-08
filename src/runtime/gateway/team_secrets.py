"""Team secrets — sealed here, opened here, and only for a sandboxed command.

A team secret (`domain.policies`) is sealed with the credential cipher, its additional
data binding it to its organization and name, so a sealed value moved to another row
or another organization does not open. `open_secrets` is called by the terminal tool
for each sandboxed command: it opens every secret, registers each value with the
scrubber — so a command that prints one returns `[REDACTED]` to the bot, its log and
its checkpoint — and hands them to the computer, which puts them in the sandbox's
environment and nowhere else.
"""

from __future__ import annotations

import uuid

from runtime.domain.scrub import registry as secret_registry
from runtime.gateway.vault import load_cipher
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings


def secret_aad(organization_id: uuid.UUID, name: str) -> bytes:
    return b"team-secret:" + organization_id.bytes + name.encode()


def seal_secret(
    settings: Settings, organization_id: uuid.UUID, name: str, value: str
) -> tuple[str, bytes, bytes]:
    return load_cipher(settings).encrypt(value, aad=secret_aad(organization_id, name))


async def open_secrets(
    uow_factory: UnitOfWorkFactory, settings: Settings, organization_id: uuid.UUID
) -> dict[str, str]:
    async with uow_factory() as uow:
        rows = await uow.team_secrets.for_organization(organization_id)
    if not rows:
        return {}
    cipher = load_cipher(settings)
    opened = {
        r.name: cipher.decrypt(
            r.key_id, r.nonce, r.ciphertext, aad=secret_aad(organization_id, r.name)
        )
        for r in rows
    }
    for name, value in opened.items():
        secret_registry().register(value, label=f"team-secret:{name}")
    return opened
