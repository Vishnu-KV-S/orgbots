"""Members: invitations, sign-in links, sessions, roles, and single sign-on.

The rules are `domain.members`; this is where they meet the database, the identity
provider (`gateway.oidc`) and the credential cipher. Nothing here is reachable without
`RUNTIME_AUTH_MODE=members` except the CLI's `add-owner`, which is how the first owner
gets in.

**Links.** A person joins by an invitation link and signs in again by a sign-in link
(an admin makes one, or the CLI) or through single sign-on. A link is used once, and
only its hash is kept. Joining or signing in makes a session: a random token in an
HttpOnly cookie, stored as a hash, good for `SESSION_TTL`. Suspending a member ends
their sessions and spends their open links at once.

**Single sign-on** is OpenID Connect. The organization is found from the email's domain
(an admin lists the domains), the browser goes to the provider with a one-use `state`,
and comes back with a code; the ID token it is exchanged for must be signed by the
provider, for this client, carry the `nonce` this sign-in made, and vouch for an email
in one of the organization's domains. A person who is not yet a member gets in only if
the admin turned on `auto_join`, and then as a `member`.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass
from typing import cast

from runtime.domain.members import (
    LINK_TTL,
    OIDC_STATE_TTL,
    SESSION_TTL,
    SIGN_IN_LINK_TTL,
    Member,
    MemberError,
    NotAllowedError,
    Role,
    check_id_claims,
    check_invite,
    check_role_change,
    check_suspend,
    clean_email,
    domain_of,
    new_token,
    token_hash,
)
from runtime.gateway.oidc import OIDCClient, OIDCError, pkce_pair
from runtime.gateway.vault import load_cipher
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.members import LinkRow, MemberRow, SSORow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory
from runtime.settings import Settings

log = get_logger("runtime.members")

_SECRET_AAD = b"sso:client-secret"


def as_member(row: MemberRow) -> Member:
    return Member(
        id=row.id,
        organization_id=row.organization_id,
        email=row.email,
        name=row.name,
        role=cast(Role, row.role),
    )


@dataclass(frozen=True)
class SignedIn:
    member: Member
    token: str
    """The session token, for the cookie. Never stored."""
    return_to: str = "/"


@dataclass(frozen=True)
class LinkInfo:
    link: LinkRow
    organization: str
    joining: bool
    """An invitation for someone not yet a member, rather than a sign-in."""


class MemberService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        *,
        oidc: OIDCClient | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings
        self._oidc = oidc or OIDCClient()

    @property
    def callback_url(self) -> str:
        return self._settings.ui_url.rstrip("/") + "/rt/v1/auth/sso/callback"

    # --- sessions --------------------------------------------------------------------

    async def _session(self, uow: UnitOfWork, member_id: uuid.UUID, user_agent: str) -> str:
        token = new_token()
        await uow.members.add_session(
            uuid.uuid4(),
            member_id,
            token_hash(token),
            expires_at=dt.datetime.now(dt.UTC) + SESSION_TTL,
            user_agent=user_agent,
        )
        return token

    async def member_for(self, token: str) -> Member | None:
        if not token or len(token) > 200:
            return None
        async with self._uow.transaction() as uow:
            row = await uow.members.session_member(token_hash(token))
        return as_member(row) if row else None

    async def sign_out(self, token: str) -> None:
        async with self._uow.transaction() as uow:
            await uow.members.drop_session(token_hash(token))

    # --- links -----------------------------------------------------------------------

    async def _link(
        self,
        uow: UnitOfWork,
        organization_id: uuid.UUID,
        *,
        kind: str,
        email: str,
        role: Role,
        member_id: uuid.UUID | None,
        made_by: uuid.UUID | None,
    ) -> tuple[LinkRow, str]:
        token = new_token()
        link_id = uuid.uuid4()
        ttl = LINK_TTL if kind == "invite" else SIGN_IN_LINK_TTL
        await uow.members.add_link(
            link_id,
            organization_id,
            kind=kind,
            email=email,
            role=role,
            member_id=member_id,
            made_by=made_by,
            token_hash=token_hash(token),
            expires_at=dt.datetime.now(dt.UTC) + ttl,
        )
        row = await uow.members.link(token_hash(token))
        assert row is not None
        return row, token

    async def add_owner(
        self, organization_id: uuid.UUID, email: str, *, name: str = ""
    ) -> tuple[MemberRow, str]:
        """The CLI's way in: an owner (made, or made an owner) and a sign-in link."""
        email = clean_email(email)
        async with self._uow.transaction() as uow:
            row = await uow.members.by_email(organization_id, email)
            if row is None:
                await uow.members.add(
                    uuid.uuid4(), organization_id, email=email, name=name, role="owner"
                )
            else:
                await uow.members.update(row.id, {"role": "owner", "status": "active"})
            row = await uow.members.by_email(organization_id, email)
            assert row is not None
            _, token = await self._link(
                uow,
                organization_id,
                kind="sign_in",
                email=email,
                role="owner",
                member_id=row.id,
                made_by=None,
            )
        return row, token

    async def operator_link(self, organization_id: uuid.UUID, email: str) -> tuple[MemberRow, str]:
        """A sign-in link for an existing member, from the CLI — the operator's way back
        in for someone who has lost their session and has no admin to ask."""
        email = clean_email(email)
        async with self._uow.transaction() as uow:
            row = await uow.members.by_email(organization_id, email)
            if row is None:
                raise MemberError(f"{email} is not a member")
            if row.status != "active":
                raise MemberError(f"{email} is suspended")
            _, token = await self._link(
                uow,
                organization_id,
                kind="sign_in",
                email=email,
                role=cast(Role, row.role),
                member_id=row.id,
                made_by=None,
            )
        return row, token

    async def invite(self, actor: Member, email: str, role: Role) -> tuple[LinkRow, str]:
        check_invite(actor, role)
        email = clean_email(email)
        async with self._uow.transaction() as uow:
            existing = await uow.members.by_email(actor.organization_id, email)
            if existing is not None:
                raise MemberError(f"{email} is already a member")
            return await self._link(
                uow,
                actor.organization_id,
                kind="invite",
                email=email,
                role=role,
                member_id=None,
                made_by=actor.id,
            )

    async def sign_in_link(self, actor: Member, member_id: uuid.UUID) -> str:
        if not actor.is_admin and actor.id != member_id:
            raise NotAllowedError("only an owner or an admin can make a sign-in link")
        async with self._uow.transaction() as uow:
            row = await uow.members.get(member_id)
            if row is None or row.organization_id != actor.organization_id:
                raise MemberError("no such member")
            if row.status != "active":
                raise MemberError(f"{row.email} is suspended")
            _, token = await self._link(
                uow,
                actor.organization_id,
                kind="sign_in",
                email=row.email,
                role=cast(Role, row.role),
                member_id=row.id,
                made_by=actor.id,
            )
        return token

    async def link_info(self, token: str) -> LinkInfo:
        async with self._uow() as uow:
            link = await uow.members.link(token_hash(token))
            if link is None:
                raise MemberError("this link isn't valid")
            name = await uow.members.organization_name(link.organization_id)
            existing = await uow.members.by_email(link.organization_id, link.email)
        if link.revoked_at is not None:
            raise MemberError("this link was turned off")
        if link.used_at is not None:
            raise MemberError("this link was already used; ask for a new one")
        if link.expires_at <= dt.datetime.now(dt.UTC):
            raise MemberError("this link has expired; ask for a new one")
        return LinkInfo(link=link, organization=name, joining=existing is None)

    async def accept(self, token: str, *, name: str = "", user_agent: str = "") -> SignedIn:
        info = await self.link_info(token)
        link = info.link
        async with self._uow.transaction() as uow:
            if not await uow.members.use_link(link.id):
                raise MemberError("this link was already used; ask for a new one")
            row = await uow.members.by_email(link.organization_id, link.email)
            if row is None:
                if link.kind != "invite":
                    raise MemberError("this sign-in link's member no longer exists")
                await uow.members.add(
                    uuid.uuid4(),
                    link.organization_id,
                    email=link.email,
                    name=name.strip()[:80],
                    role=link.role,
                )
                row = await uow.members.by_email(link.organization_id, link.email)
                assert row is not None
            elif row.status != "active":
                raise MemberError(f"{row.email} is suspended in this organization")
            elif name.strip() and not row.name:
                await uow.members.update(row.id, {"name": name.strip()[:80]})
            session = await self._session(uow, row.id, user_agent)
            row = await uow.members.get(row.id)
        assert row is not None
        log.info("member.signed_in", member_id=str(row.id), via=link.kind)
        return SignedIn(member=as_member(row), token=session)

    async def revoke_invite(self, actor: Member, link_id: uuid.UUID) -> None:
        if not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can revoke invitations")
        async with self._uow.transaction() as uow:
            if not await uow.members.revoke_link(actor.organization_id, link_id):
                raise MemberError("no such open invitation")

    async def open_invites(self, actor: Member) -> list[LinkRow]:
        if not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can see invitations")
        async with self._uow() as uow:
            return await uow.members.open_invites(actor.organization_id)

    # --- roles -----------------------------------------------------------------------

    async def members(self, organization_id: uuid.UUID) -> list[MemberRow]:
        async with self._uow() as uow:
            return await uow.members.for_organization(organization_id)

    async def _target(self, uow: UnitOfWork, actor: Member, member_id: uuid.UUID) -> MemberRow:
        row = await uow.members.get(member_id)
        if row is None or row.organization_id != actor.organization_id:
            raise MemberError("no such member")
        return row

    async def set_role(self, actor: Member, member_id: uuid.UUID, role: Role) -> MemberRow:
        async with self._uow.transaction() as uow:
            row = await self._target(uow, actor, member_id)
            owners = await uow.members.owners(actor.organization_id)
            check_role_change(actor, cast(Role, row.role), role, owners)
            await uow.members.update(member_id, {"role": role})
            changed = await uow.members.get(member_id)
        assert changed is not None
        return changed

    async def set_active(self, actor: Member, member_id: uuid.UUID, active: bool) -> MemberRow:
        async with self._uow.transaction() as uow:
            row = await self._target(uow, actor, member_id)
            if active:
                if not actor.is_admin:
                    raise NotAllowedError("only an owner or an admin can restore members")
                if row.role == "owner" and actor.role != "owner":
                    raise NotAllowedError("only an owner can restore an owner")
                await uow.members.update(member_id, {"status": "active"})
            else:
                owners = await uow.members.owners(actor.organization_id)
                check_suspend(actor, row.id, cast(Role, row.role), owners)
                await uow.members.update(member_id, {"status": "suspended"})
                await uow.members.drop_sessions(member_id)
                await uow.members.revoke_links_for(actor.organization_id, row.email)
            changed = await uow.members.get(member_id)
        assert changed is not None
        return changed

    # --- single sign-on ----------------------------------------------------------------

    async def sso(self, organization_id: uuid.UUID) -> SSORow | None:
        async with self._uow() as uow:
            return await uow.members.sso(organization_id)

    async def save_sso(
        self,
        actor: Member,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        domains: list[str],
        auto_join: bool,
        enabled: bool,
    ) -> SSORow:
        """Check the provider answers as itself, then save. The secret is sealed and never
        returned; `client_secret` None keeps the one saved."""
        if not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can set up single sign-on")
        clean = sorted({d.strip().lower().lstrip("@") for d in domains if d.strip()})
        if not clean:
            raise MemberError("list at least one email domain that signs in through it")
        for d in clean:
            if "." not in d or " " in d:
                raise MemberError(f"{d!r} is not an email domain")
        try:
            provider = await self._oidc.discover(issuer)
        except OIDCError as exc:
            raise MemberError(str(exc)) from exc
        existing = await self.sso(actor.organization_id)
        if client_secret is None and (existing is None or existing.secret_ciphertext is None):
            raise MemberError("the client secret is needed the first time")
        sealed = None
        if client_secret is not None:
            cipher = load_cipher(self._settings)
            sealed = cipher.encrypt(client_secret, aad=_SECRET_AAD + actor.organization_id.bytes)
        async with self._uow.transaction() as uow:
            await uow.members.save_sso(
                actor.organization_id,
                issuer=provider.issuer.rstrip("/"),
                client_id=client_id.strip(),
                secret=sealed,
                domains=clean,
                auto_join=auto_join,
                enabled=enabled,
            )
            row = await uow.members.sso(actor.organization_id)
        assert row is not None
        return row

    async def delete_sso(self, actor: Member) -> None:
        if not actor.is_admin:
            raise NotAllowedError("only an owner or an admin can turn off single sign-on")
        async with self._uow.transaction() as uow:
            await uow.members.delete_sso(actor.organization_id)

    async def sso_start(self, email: str, *, return_to: str = "/") -> str:
        """Where to send the browser to sign `email` in, or `MemberError`."""
        email = clean_email(email)
        async with self._uow() as uow:
            config = await uow.members.sso_for_domain(domain_of(email))
        if config is None:
            raise MemberError(
                "single sign-on isn't set up for that email's domain; "
                "ask an admin for a sign-in link"
            )
        try:
            provider = await self._oidc.discover(config.issuer)
        except OIDCError as exc:
            raise MemberError(str(exc)) from exc
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        async with self._uow.transaction() as uow:
            await uow.members.add_state(
                state,
                config.organization_id,
                nonce=nonce,
                verifier=verifier,
                return_to=_local_path(return_to),
            )
        return self._oidc.authorize_url(
            provider,
            client_id=config.client_id,
            redirect_uri=self.callback_url,
            state=state,
            nonce=nonce,
            challenge=challenge,
            login_hint=email,
        )

    async def sso_finish(self, state: str, code: str, *, user_agent: str = "") -> SignedIn:
        async with self._uow.transaction() as uow:
            pending = await uow.members.take_state(state)
        if pending is None or dt.datetime.now(dt.UTC) - pending.created_at > OIDC_STATE_TTL:
            raise MemberError("this sign-in has expired or was already used; start again")
        config = await self.sso(pending.organization_id)
        if config is None or not config.enabled or config.secret_ciphertext is None:
            raise MemberError("single sign-on is no longer set up here")
        cipher = load_cipher(self._settings)
        secret = cipher.decrypt(
            config.secret_key_id or "",
            config.secret_nonce or b"",
            config.secret_ciphertext,
            aad=_SECRET_AAD + config.organization_id.bytes,
        )
        try:
            provider = await self._oidc.discover(config.issuer)
            claims = await self._oidc.exchange(
                provider,
                client_id=config.client_id,
                client_secret=secret,
                code=code,
                redirect_uri=self.callback_url,
                verifier=pending.verifier,
            )
        except OIDCError as exc:
            raise MemberError(str(exc)) from exc
        email = check_id_claims(
            claims,
            issuer=provider.issuer,
            client_id=config.client_id,
            nonce=pending.nonce,
            now=dt.datetime.now(dt.UTC),
        )
        if domain_of(email) not in config.domains:
            raise MemberError(f"{email} isn't in a domain that signs in here")
        org = config.organization_id
        async with self._uow.transaction() as uow:
            row = await uow.members.by_email(org, email)
            if row is None:
                if not config.auto_join:
                    raise MemberError(
                        f"{email} isn't a member here yet; ask an admin to invite you"
                    )
                name = str(claims.get("name") or "")[:80]
                await uow.members.add(uuid.uuid4(), org, email=email, name=name, role="member")
                row = await uow.members.by_email(org, email)
                assert row is not None
            elif row.status != "active":
                raise MemberError(f"{email} is suspended in this organization")
            session = await self._session(uow, row.id, user_agent)
        log.info("member.signed_in", member_id=str(row.id), via="sso")
        return SignedIn(member=as_member(row), token=session, return_to=pending.return_to)


def _local_path(value: str) -> str:
    """Only a path on this site — never `//elsewhere` or a full URL, which would make the
    sign-in an open redirect."""
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:500]
