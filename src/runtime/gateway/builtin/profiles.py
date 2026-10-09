"""Which browser profile a run's bot works in — for the computer tools.

A tool gets its run's id and nothing that could reach the database (`ToolContext`), so
the tools that drive the computer are built with a unit of work and ask here. The answer
comes from the run, not from the tool's arguments: the run's actor is the bot, and the
bot's owner and sharing decide its profile (`domain.members.computer_profile`). A run
that is not a bot's — a test, a department's agent — works in the default profile.
"""

from __future__ import annotations

import uuid

from runtime.domain.members import computer_profile
from runtime.gateway.tools import ToolContext
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
