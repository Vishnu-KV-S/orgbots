"""The login vault: where a person's sign-in details are sealed and opened.

**Two doors in, one door out.** In: a person submitting the credential card (the API),
and nothing else — no tool writes here, so a bot cannot plant a login. Out: `open`,
called by `browser.act@1` when it fills a form, and nothing else. The graph that
decides *whether* to fill sees `VaultOption`s — ids, kinds, a hint — and the plaintext
goes from `open` to the computer over loopback and from there into the page, never
into a return value a run keeps.

**The checks that matter are made at the door out.** `open` refuses an entry from
another organization, an expired one-time value, one given to a different bot, and —
the one that stops a manipulated bot sending a bank password to a lookalike — an
entry whose site is not the host it is being opened for. The computer then checks the
page's host again at the moment it types, because a page can navigate between this
check and that one.

**Encryption is the credentials table's**, the same `CredentialCipher` and keys
(`RUNTIME_CREDENTIAL_KEYS`), with AAD binding each ciphertext to its organization, row
and site (`runtime.domain.vault.vault_aad`). Every value opened is registered with the
scrubber, so a site that echoes your email back in a page is redacted before the
observation reaches the bot.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from runtime.domain.errors import MissingCredentials
from runtime.domain.scrub import registry as secret_registry
from runtime.domain.vault import (
    IDENTITY_KINDS,
    ONCE_TTL_S,
    CredentialField,
    hint,
    site_of,
    split_values,
    vault_aad,
)
from runtime.gateway.credentials import CredentialCipher
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.vault import VaultEntryRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory
from runtime.settings import Settings

log = get_logger("gateway.vault")


class VaultUnavailableError(MissingCredentials):
    """No encryption key is configured. The message says how to set one."""


class VaultRefusedError(Exception):
    """An entry that may not be opened here. The message is shown to the bot — it
    names the entry's site and never a value."""


def load_cipher(settings: Settings | None = None) -> CredentialCipher:
    """The credential cipher, from the process environment or `.env`.

    `CredentialCipher.from_env` reads `os.environ` alone, so a key that is only in
    `.env` leaves the credential broker disabled until the file is exported. The
    vault is opened by the API as well as the worker, and a vault that works in one
    process and not the other would be a card that accepts a password and a bot that
    cannot use it. So `Settings`' copy (which does read `.env`) fills in what the
    environment lacks; the environment still wins when both are set.
    """
    env = dict(os.environ)
    if settings is not None:
        if settings.credential_keys and not env.get("RUNTIME_CREDENTIAL_KEYS"):
            env["RUNTIME_CREDENTIAL_KEYS"] = settings.credential_keys
        if settings.credential_active_key and not env.get("RUNTIME_CREDENTIAL_ACTIVE_KEY"):
            env["RUNTIME_CREDENTIAL_ACTIVE_KEY"] = settings.credential_active_key
    try:
        return CredentialCipher.from_env(env)
    except MissingCredentials as exc:
        raise VaultUnavailableError(
            "the login vault needs an encryption key and none is configured. "
            + str(exc)
            + "\nthen put it in .env as RUNTIME_CREDENTIAL_KEYS=k1:<key> and restart the "
            "API and the worker."
        ) from exc


@dataclass(frozen=True, slots=True)
class Submitted:
    once_id: uuid.UUID
    saved_id: uuid.UUID | None
    label: str


class Vault:
    def __init__(self, uow_factory: UnitOfWorkFactory, cipher: CredentialCipher) -> None:
        self._uow = uow_factory
        self._cipher = cipher

    @classmethod
    def from_settings(cls, uow_factory: UnitOfWorkFactory, settings: Settings) -> Vault:
        return cls(uow_factory, load_cipher(settings))

    # --- sealing -------------------------------------------------------------------

    def _seal(
        self, organization_id: uuid.UUID, entry_id: uuid.UUID, host: str, values: Mapping[str, str]
    ) -> tuple[str, bytes, bytes]:
        return self._cipher.encrypt(
            json.dumps(dict(values), sort_keys=True),
            aad=vault_aad(organization_id, entry_id, host),
        )

    def _unseal(self, row: VaultEntryRow) -> dict[str, str]:
        raw = self._cipher.decrypt(
            row.key_id,
            row.nonce,
            row.ciphertext,
            aad=vault_aad(row.organization_id, row.id, row.host),
        )
        values = json.loads(raw)
        return {str(k): str(v) for k, v in values.items()}

    async def submit(
        self,
        uow: UnitOfWork,
        organization_id: uuid.UUID,
        *,
        bot_id: uuid.UUID,
        host: str,
        fields: list[CredentialField],
        submitted: Mapping[str, str],
        save: bool,
    ) -> Submitted:
        """Seal what a person typed into the card, in the caller's transaction.

        Always a one-time entry for this bot, holding everything — the fill and the
        next page of it use that. With `save`, the identity and password also go into
        the organization's saved login for the site: the existing one for the same
        account if there is one (a changed password replaces the old), else a new one.
        """
        site = site_of(host)
        once, saved = split_values(fields, dict(submitted))
        label = hint(once)
        await uow.vault.purge_expired()

        once_id = uuid.uuid4()
        key_id, nonce, ct = self._seal(organization_id, once_id, site, once)
        await uow.vault.add_entry(
            once_id,
            organization_id,
            host=site,
            kind="once",
            bot_id=bot_id,
            label=label,
            kinds=[k for k in once if not k.startswith("field:")],
            key_id=key_id,
            nonce=nonce,
            ciphertext=ct,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ONCE_TTL_S),
        )

        saved_id: uuid.UUID | None = None
        if save and saved:
            saved_id = await self._keep(uow, organization_id, site, saved, bot_id=bot_id)
        log.info(
            "vault.submitted",
            host=site,
            kinds=sorted(once),
            saved=saved_id is not None,
            bot_id=str(bot_id),
        )
        return Submitted(once_id=once_id, saved_id=saved_id, label=label)

    async def _keep(
        self,
        uow: UnitOfWork,
        organization_id: uuid.UUID,
        site: str,
        values: dict[str, str],
        *,
        bot_id: uuid.UUID,
    ) -> uuid.UUID:
        identity = _identity(values)
        for row in await uow.vault.options(organization_id, site, bot_id):
            if row.kind != "saved":
                continue
            if identity and _identity(self._unseal(row)) == identity:
                key_id, nonce, ct = self._seal(organization_id, row.id, site, values)
                await uow.vault.replace_secret(
                    row.id,
                    label=hint(values),
                    kinds=list(values),
                    key_id=key_id,
                    nonce=nonce,
                    ciphertext=ct,
                )
                return row.id
        entry_id = uuid.uuid4()
        key_id, nonce, ct = self._seal(organization_id, entry_id, site, values)
        await uow.vault.add_entry(
            entry_id,
            organization_id,
            host=site,
            kind="saved",
            bot_id=bot_id,
            label=hint(values),
            kinds=list(values),
            key_id=key_id,
            nonce=nonce,
            ciphertext=ct,
            expires_at=None,
        )
        return entry_id

    # --- opening -------------------------------------------------------------------

    async def open(
        self,
        organization_id: uuid.UUID,
        entry_ids: list[uuid.UUID],
        *,
        bot_id: uuid.UUID,
    ) -> tuple[str, dict[str, str]]:
        """`(site, values)` for a fill. Earlier entries win where two answer one kind.

        The site comes from the entries, not from the caller: the fill types into the
        page only if the page is on this site, and a caller-supplied host would let
        whoever built the action choose which site the password goes to.
        """
        if not entry_ids:
            raise VaultRefusedError("nothing to fill with")
        merged: dict[str, str] = {}
        site = ""
        now = dt.datetime.now(dt.UTC)
        async with self._uow() as uow:
            rows = [await uow.vault.get_entry(entry_id) for entry_id in entry_ids]
        for entry_id, row in zip(entry_ids, rows, strict=True):
            if row is None or row.organization_id != organization_id:
                raise VaultRefusedError(f"vault entry {entry_id} does not exist (deleted?)")
            if row.kind == "once":
                if row.bot_id != bot_id:
                    raise VaultRefusedError("those sign-in details were given to another bot")
                if row.expires_at is not None and row.expires_at <= now:
                    raise VaultRefusedError(
                        f"the sign-in details for {row.host} expired; ask the person again"
                    )
            if site and row.host != site:
                raise VaultRefusedError("vault entries for two different sites in one fill")
            site = row.host
            for key, value in self._unseal(row).items():
                merged.setdefault(key, value)
        for key, value in merged.items():
            secret_registry().register(value, label=f"vault:{site}:{key.split(':')[0]}")
        async with self._uow.transaction() as uow:
            await uow.vault.mark_used(list(entry_ids))
        log.info("vault.opened", host=site, entries=[str(e) for e in entry_ids], bot_id=str(bot_id))
        return site, merged

    # --- managing ------------------------------------------------------------------

    async def saved(self, organization_id: uuid.UUID) -> list[VaultEntryRow]:
        async with self._uow() as uow:
            return await uow.vault.saved(organization_id)


def _identity(values: Mapping[str, str]) -> str:
    for kind in sorted(IDENTITY_KINDS):
        value = values.get(kind, "").strip().lower()
        if value:
            return value
    return ""
