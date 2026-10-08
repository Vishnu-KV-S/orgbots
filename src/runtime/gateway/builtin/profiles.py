"""Which browser profile a run's bot works in, and its organization's network — for the
computer tools.

A tool gets its run's id and nothing that could reach the database (`ToolContext`), so
the tools that drive the computer are built with a unit of work and ask here. The answer
comes from the run, not from the tool's arguments: the run's actor is the bot, and the
bot's owner and sharing decide its profile (`domain.members.computer_profile`). A run
that is not a bot's — a test, a department's agent — works in the default profile.
"""

from __future__ import annotations

import time
import uuid

from runtime.domain.members import computer_profile
from runtime.domain.policies import Policy
from runtime.gateway.tools import ToolContext
from runtime.org.audit import record
from runtime.persistence.repositories.bots import BotRow
from runtime.persistence.uow import UnitOfWorkFactory


async def run_bot(uow_factory: UnitOfWorkFactory | None, ctx: ToolContext | None) -> BotRow | None:
    if uow_factory is None or ctx is None:
        return None
    try:
        run_id = uuid.UUID(ctx.run_id)
    except ValueError:
        return None
    async with uow_factory() as uow:
        return await uow.bots.for_run(run_id)


def profile_of(bot: BotRow | None) -> str:
    return computer_profile(bot) if bot is not None else ""


_POLICY_S = 15.0
_policies: dict[str, tuple[float, Policy]] = {}


async def org_policy(uow_factory: UnitOfWorkFactory | None, ctx: ToolContext | None) -> Policy:
    """The run's organization's policy, cached for a few seconds — an admin's change
    reaches every bot within `_POLICY_S`."""
    if uow_factory is None or ctx is None:
        return Policy()
    cached = _policies.get(ctx.organization_id)
    if cached and time.monotonic() - cached[0] < _POLICY_S:
        return cached[1]
    try:
        org = uuid.UUID(ctx.organization_id)
    except ValueError:
        return Policy()
    async with uow_factory() as uow:
        policy = await uow.policies.get(org)
    _policies[ctx.organization_id] = (time.monotonic(), policy)
    return policy


def allow_header(policy: Policy) -> dict[str, str]:
    """`X-Allow-Hosts` for the computer: `*`, or the hosts its browser may reach."""
    return {"x-allow-hosts": "*" if policy.network == "open" else ",".join(policy.allowed_hosts)}


def refusal(policy: Policy, url: str) -> str | None:
    if policy.url_allowed(url):
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return (
        f"your organization's network allowlist does not include {host}; only "
        f"{', '.join(policy.allowed_hosts) or 'no hosts'} can be reached — tell your person"
    )


async def record_block(
    uow_factory: UnitOfWorkFactory | None, ctx: ToolContext | None, url: str, tool: str
) -> None:
    """The allowlist stopped a bot: a line in the organization's audit trail
    (`network.blocked`), so admins — and their collector — see the guardrail act."""
    if uow_factory is None or ctx is None:
        return
    host = url.split("://", 1)[-1].split("/", 1)[0][:253]
    bot = await run_bot(uow_factory, ctx)
    async with uow_factory.transaction() as uow:
        await record(
            uow,
            uuid.UUID(ctx.organization_id),
            "network.blocked",
            actor_label=f"bot:{bot.name}" if bot else "bot",
            target=host,
            detail={"tool": tool, "run_id": ctx.run_id},
        )
