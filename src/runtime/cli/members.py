"""`python -m runtime.cli members` — the people who sign in, from the operator's side.

With `RUNTIME_AUTH_MODE=members` nobody gets in without a session, and sessions come
from sign-in links or single sign-on — which an owner sets up. This is how the first
owner gets in:

    members add-owner you@company.com --name "You"   → prints a sign-in link
    members link someone@company.com                 → a new link for an existing member
    members list

The organization is `--organization`, or the one the UI uses
(`RUNTIME_BOTS_ORGANIZATION_ID`). A link is printed once and works once, for a day; its
hash is all the database keeps. Anyone who can run this can sign in as anyone — it is
the operator's key, like `killswitch`.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

from runtime.domain.members import MemberError
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings


def add_members_parser(sub: Any) -> None:
    members = sub.add_parser("members", help="people who sign in (RUNTIME_AUTH_MODE=members)")
    verbs = members.add_subparsers(dest="verb", required=True)
    owner = verbs.add_parser("add-owner", help="make EMAIL an owner and print a sign-in link")
    owner.add_argument("email")
    owner.add_argument("--name", default="")
    owner.set_defaults(fn=cmd_add_owner, needs_org=False)
    link = verbs.add_parser("link", help="print a new sign-in link for an existing member")
    link.add_argument("email")
    link.set_defaults(fn=cmd_link, needs_org=False)
    listing = verbs.add_parser("list", help="list the organization's members")
    listing.set_defaults(fn=cmd_list, needs_org=False)


def _org(args: argparse.Namespace, settings: Settings) -> uuid.UUID:
    return uuid.UUID(getattr(args, "organization", None) or settings.bots_organization_id)


def _link(settings: Settings, token: str) -> str:
    return f"{settings.ui_url.rstrip('/')}/signin?link={token}"


def _warn_mode(settings: Settings) -> None:
    if settings.auth_mode != "members":
        print(
            "note: RUNTIME_AUTH_MODE is not `members`, so the runtime does not ask anyone "
            "to sign in yet",
            file=sys.stderr,
        )


async def cmd_add_owner(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.runtime.bootstrap import Registrar
    from runtime.runtime.members import MemberService

    uow = UnitOfWorkFactory(settings)
    org = _org(args, settings)
    await Registrar(uow).ensure_organization(org, settings.bots_organization_name)  # type: ignore[arg-type]
    try:
        row, token = await MemberService(uow, settings).add_owner(org, args.email, name=args.name)
    except MemberError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _warn_mode(settings)
    print(
        f"{row.email} is an owner of {org}. Sign in within 24 hours at:\n{_link(settings, token)}"
    )
    return 0


async def cmd_link(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.runtime.members import MemberService

    uow = UnitOfWorkFactory(settings)
    try:
        row, token = await MemberService(uow, settings).operator_link(
            _org(args, settings), args.email
        )
    except MemberError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _warn_mode(settings)
    print(f"Sign-in link for {row.email} (works once, for 24 hours):\n{_link(settings, token)}")
    return 0


async def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    async with uow() as session:
        rows = await session.members.for_organization(_org(args, settings))
    if not rows:
        print("no members yet — `members add-owner EMAIL` makes the first")
        return 0
    for r in rows:
        seen = r.last_seen_at.strftime("%Y-%m-%d %H:%M") if r.last_seen_at else "never"
        print(f"{r.role:<7} {r.status:<9} {r.email:<40} {r.name or '—':<24} last seen {seen}")
    return 0
