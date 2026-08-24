"""The credential broker.

I4 says the gateway is the only place credentials live, and until M2 that was true by
absence: no tool needed one, and the two that did read `Settings`. Reading an env var
is not a credential model — it cannot be rotated under a running process, cannot be
scoped per connection, and leaves no record of who read what.

**Fetched per call, never pinned.** The broker reads the active row on every call and
decrypts it. That is one indexed read plus an AES-GCM open, both microseconds, and it
is what makes T37 hold: a rotation lands, the next call uses the new secret, and
nothing in flight is holding a stale one. A cache here would be a correctness bug
wearing a performance costume — the whole reason to have rotation is that a
compromised key must stop working *now*.

**Plaintext never leaves this module unregistered.** Every value handed out is
registered with `runtime.domain.scrub`, so if a provider echoes our key back in an
error message, the scrubber recognises it by exact match before it reaches state, a
checkpoint, a log or an artifact (T38). That is the mechanism that actually works;
the regex patterns in the scrubber are a backstop for other people's secrets.

**Where the key lives.** `RUNTIME_CREDENTIAL_KEYS` maps key ids to base64 32-byte
keys, and `RUNTIME_CREDENTIAL_ACTIVE_KEY` names which one encrypts new rows. Two of
them can be live at once, which is what makes *key* rotation separable from
*credential* rotation: re-key in the background, flip the active id, retire the old
one when nothing references it. An attacker with the database has ciphertext; an
attacker with the database and the process environment has secrets. Moving the key to
a KMS changes this class and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from runtime.domain.errors import DecryptionFailed, MissingCredentials
from runtime.domain.ids import OrganizationId
from runtime.domain.scrub import registry as secret_registry
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("gateway.credentials")

NONCE_BYTES = 12
"""AES-GCM's standard nonce size. Random per encryption, stored beside the
ciphertext — never derived from the credential name, which would repeat a nonce
across rotations of the same credential and is the one way to break GCM outright."""


def _b64key(raw: str) -> bytes:
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError(f"credential key must be 32 bytes (256 bits), got {len(key)}")
    return key


@dataclass(frozen=True, slots=True)
class Credential:
    """A decrypted secret, plus enough metadata to attribute it in a log line."""

    name: str
    provider: str
    version: int
    secret: str
    fingerprint: str

    def __repr__(self) -> str:
        """No secret in the repr. A dataclass's default repr would put the key in
        every traceback, and a traceback is the most widely copied artifact there
        is."""
        return (
            f"Credential(name={self.name!r}, provider={self.provider!r}, "
            f"version={self.version}, fingerprint={self.fingerprint!r})"
        )


def fingerprint(secret: str) -> str:
    """Enough to tell two secrets apart in a log; not enough to use one.

    A truncated SHA-256 of the secret. Truncation is the point: 12 hex characters
    identifies a rotation in an incident timeline and offers nothing to an attacker
    reading the same log.
    """
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


class CredentialCipher:
    """AES-256-GCM over the credential column. Holds keys; knows nothing about rows."""

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError(f"active key id {active_key_id!r} is not among the loaded keys")
        self._keys = keys
        self._active = active_key_id

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CredentialCipher:
        """Load from `RUNTIME_CREDENTIAL_KEYS` (`id:base64,id:base64`).

        Raises rather than generating a key when none is configured. A process that
        invented its own key would encrypt rows nothing else can read, and the failure
        would surface later as a decryption error on a different host — which is a
        much worse morning than a refusal to start.
        """
        source = env if env is not None else dict(os.environ)
        raw = source.get("RUNTIME_CREDENTIAL_KEYS", "").strip()
        if not raw:
            raise MissingCredentials(
                "RUNTIME_CREDENTIAL_KEYS is unset. Generate one with:\n"
                "  python -c \"import base64,os;print('k1:'+base64.b64encode("
                'os.urandom(32)).decode())"'
            )
        keys: dict[str, bytes] = {}
        for part in raw.split(","):
            key_id, _, material = part.strip().partition(":")
            if not key_id or not material:
                raise ValueError(f"malformed entry in RUNTIME_CREDENTIAL_KEYS: {part!r}")
            keys[key_id] = _b64key(material)
        active = source.get("RUNTIME_CREDENTIAL_ACTIVE_KEY", "").strip() or next(iter(keys))
        return cls(keys, active)

    @property
    def active_key_id(self) -> str:
        return self._active

    def encrypt(self, plaintext: str, *, aad: bytes) -> tuple[str, bytes, bytes]:
        """`(key_id, nonce, ciphertext)`.

        `aad` binds the ciphertext to its row identity — organization and credential
        name — so a row copied from one organization's table to another's fails to
        open rather than silently authenticating as somebody else.
        """
        nonce = os.urandom(NONCE_BYTES)
        ct = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext.encode(), aad)
        return self._active, nonce, ct

    def decrypt(self, key_id: str, nonce: bytes, ciphertext: bytes, *, aad: bytes) -> str:
        key = self._keys.get(key_id)
        if key is None:
            raise DecryptionFailed(
                f"credential was encrypted under key {key_id!r}, which this process was "
                f"not given (has: {sorted(self._keys)}). This is a deployment fault: the "
                "key was rotated out before every row referencing it was re-keyed."
            )
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad).decode()
        except InvalidTag as exc:
            raise DecryptionFailed(
                f"credential under key {key_id!r} failed authentication. Either the row "
                "was tampered with or it was moved between organizations."
            ) from exc


def _aad(organization_id: OrganizationId, name: str) -> bytes:
    return f"{organization_id}:{name}".encode()


class CredentialBroker:
    """Fetches and decrypts credentials. One read per call, by design."""

    def __init__(self, uow_factory: UnitOfWorkFactory, cipher: CredentialCipher) -> None:
        self._uow = uow_factory
        self._cipher = cipher

    async def fetch(self, organization_id: OrganizationId, name: str) -> Credential:
        """The active credential, decrypted. Raises if there is none.

        Every fetch registers the plaintext with the scrubber before returning it. The
        registration is idempotent and cheap, and doing it here — rather than at the
        call sites — means a new tool cannot forget to, which is exactly the kind of
        thing a new tool forgets.
        """
        async with self._uow() as uow:
            row = await uow.credentials.active(organization_id, name)
        if row is None:
            raise MissingCredentials(
                f"no ACTIVE credential named {name!r} for organization {organization_id}. "
                "Add one with `python -m runtime.cli credentials put`."
            )
        secret = self._cipher.decrypt(
            row.key_id, row.nonce, row.ciphertext, aad=_aad(organization_id, name)
        )
        secret_registry().register(secret, label=f"credential:{name}@v{row.version}")
        return Credential(
            name=row.name,
            provider=row.provider,
            version=row.version,
            secret=secret,
            fingerprint=row.fingerprint,
        )

    async def try_fetch(
        self, organization_id: OrganizationId, name: str | None
    ) -> Credential | None:
        """`fetch`, but `None` for a tool that declares no credential. Not a fallback
        for a *missing* one — that still raises, because a tool that needs a key and
        silently proceeds without it produces a failure at the provider that looks like
        the provider's fault."""
        if not name:
            return None
        return await self.fetch(organization_id, name)

    async def try_fetch_optional(
        self, organization_id: OrganizationId, name: str
    ) -> Credential | None:
        """The credential, or `None` if the store has no row for it.

        Distinct from `try_fetch`, which raises on a missing row. This one is for the
        model provider, where an absent row means "use whatever you resolved for
        yourself" — a `DEEPSEEK_API_KEY` in the environment — and that is the
        development path M2 deliberately leaves open. A *broken* row still raises,
        because "no credential configured" and "the configured credential will not
        decrypt" need different fixes.
        """
        async with self._uow() as uow:
            row = await uow.credentials.active(organization_id, name)
        if row is None:
            return None
        secret = self._cipher.decrypt(
            row.key_id, row.nonce, row.ciphertext, aad=_aad(organization_id, name)
        )
        secret_registry().register(secret, label=f"credential:{name}@v{row.version}")
        return Credential(
            name=row.name,
            provider=row.provider,
            version=row.version,
            secret=secret,
            fingerprint=row.fingerprint,
        )

    # --- rotation ------------------------------------------------------------------

    async def put(
        self,
        organization_id: OrganizationId,
        *,
        name: str,
        provider: str,
        secret: str,
        rotated_by: str = "operator",
    ) -> int:
        """Store a new version and retire the previous one. Returns the new version.

        Retire-then-insert inside one transaction. The partial unique index
        `uq_credential_active` makes the window in which two versions are ACTIVE not
        merely short but non-existent: a concurrent rotation blocks on the row lock
        taken by the retire, then inserts a version after it rather than beside it.
        """
        key_id, nonce, ciphertext = self._cipher.encrypt(secret, aad=_aad(organization_id, name))
        async with self._uow.transaction() as uow:
            version = await uow.credentials.next_version(organization_id, name)
            retired = await uow.credentials.retire(organization_id, name, by=rotated_by)
            await uow.credentials.put(
                uuid.uuid4(),
                organization_id,
                name=name,
                provider=provider,
                version=version,
                key_id=key_id,
                nonce=nonce,
                ciphertext=ciphertext,
                fingerprint=fingerprint(secret),
            )
        log.info(
            "credential.rotated" if retired else "credential.created",
            name=name,
            provider=provider,
            version=version,
            key_id=key_id,
            fingerprint=fingerprint(secret),
        )
        return version
