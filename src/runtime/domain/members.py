"""Members — the people of an organization, and what each may do with whose bots.

With `RUNTIME_AUTH_MODE=none` (the default) the runtime has one person and no sign-in:
whoever reaches the API is them, as it always was. With `members` it has people, and
these are the rules every API call is held to.

**Roles.** An `owner` can do everything, including making and unmaking owners. An
`admin` runs the organization — members, sign-in, the organization's apps — but cannot
change an owner. A `member` has their own bots and the team's.

**Bots.** A bot a member makes is `private`: only they see it. Shared with the `team`,
every member can talk to it, approve its steps once, and watch its screen — but only
its owner (or an admin) changes what it *is*: its profile, brief, rules, routines and
memory, and whether it stays shared. A bot with no owner was made before members
existed (or by the runtime itself) and is everyone's.

**Browser profiles.** A bot signs in to sites in a browser profile, and a profile's
cookies and saved logins are whoever's it is. A member's private bots share the member's
profile; a team bot has its own, so a teammate's bot is never signed in as you. Without
members there is one profile, `""`, as there always was.

Tokens (sessions, sign-in links) are random, shown once, and kept only as SHA-256.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["owner", "admin", "member"]
Visibility = Literal["private", "team"]

ROLES: tuple[Role, ...] = ("owner", "admin", "member")
SESSION_TTL = dt.timedelta(days=14)
LINK_TTL = dt.timedelta(days=7)
SIGN_IN_LINK_TTL = dt.timedelta(hours=24)
OIDC_STATE_TTL = dt.timedelta(minutes=10)
SESSION_COOKIE = "aor_session"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MemberError(ValueError):
    """A request the rules refuse. The message is shown to the person."""


class NotAllowedError(MemberError):
    """Signed in, but not allowed to do this."""


@dataclass(frozen=True, slots=True)
class Member:
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    name: str
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")

    @property
    def shown(self) -> str:
        return self.name or self.email


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise MemberError(f"{value!r} is not an email address")
    return email


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[1]


# --- bots -------------------------------------------------------------------------------


def can_see(member: Member | None, bot: Any) -> bool:
    """`member` None is a runtime without members: everything is the one person's."""
    if member is None:
        return True
    if bot.organization_id != member.organization_id:
        return False
    owner = getattr(bot, "owner_member_id", None)
    return owner is None or owner == member.id or getattr(bot, "visibility", "") == "team"


def can_edit(member: Member | None, bot: Any) -> bool:
    if member is None:
        return True
    if not can_see(member, bot):
        return False
    owner = getattr(bot, "owner_member_id", None)
    return owner is None or owner == member.id or member.is_admin


def computer_profile(bot: Any) -> str:
    """The browser profile (and workspace) a bot's screen runs in."""
    owner = getattr(bot, "owner_member_id", None)
    if owner is None:
        return ""
    if getattr(bot, "visibility", "private") == "team":
        return f"t-{bot.team_id.hex}"
    return f"m-{owner.hex}"


PROFILE_PATTERN = r"^(|[mt]-[0-9a-f]{32})$"
"""What a profile key looks like — checked by the computer before it becomes a path."""


# --- roles ------------------------------------------------------------------------------


def check_role_change(actor: Member, target_role: Role, new_role: Role, owners: int) -> None:
    """May `actor` move someone from `target_role` to `new_role`? `owners` counts the
    organization's active owners now."""
    if not actor.is_admin:
        raise NotAllowedError("only an owner or an admin can change roles")
    if "owner" in (target_role, new_role) and actor.role != "owner":
        raise NotAllowedError("only an owner can make or unmake an owner")
    if target_role == "owner" and new_role != "owner" and owners <= 1:
        raise MemberError("an organization needs at least one owner")


def check_suspend(actor: Member, target_id: uuid.UUID, target_role: Role, owners: int) -> None:
    if not actor.is_admin:
        raise NotAllowedError("only an owner or an admin can remove members")
    if target_id == actor.id:
        raise MemberError("you can't remove yourself")
    if target_role == "owner" and actor.role != "owner":
        raise NotAllowedError("only an owner can remove an owner")
    if target_role == "owner" and owners <= 1:
        raise MemberError("an organization needs at least one owner")


def check_invite(actor: Member, role: Role) -> None:
    if not actor.is_admin:
        raise NotAllowedError("only an owner or an admin can invite people")
    if role == "owner" and actor.role != "owner":
        raise NotAllowedError("only an owner can invite an owner")


# --- single sign-on -----------------------------------------------------------------------


def check_id_claims(
    claims: dict[str, Any], *, issuer: str, client_id: str, nonce: str, now: dt.datetime
) -> str:
    """The verified email an ID token vouches for, or `MemberError`. The signature, `exp`
    and `aud` were checked when the token was decoded; these are the rest of OpenID
    Connect Core §3.1.3.7 that a sign-in depends on."""
    if claims.get("iss") != issuer:
        raise MemberError("the identity provider's answer was for another issuer")
    aud = claims.get("aud")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise MemberError("the identity provider's answer was for another app")
    if claims.get("nonce") != nonce:
        raise MemberError("the sign-in could not be matched to this browser; start again")
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise MemberError("the identity provider did not say who you are (no email)")
    if claims.get("email_verified") is False:
        raise MemberError("the identity provider has not verified that email address")
    return clean_email(email)
