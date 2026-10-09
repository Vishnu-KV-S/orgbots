"""Templates out of bots, and links to them — the rules are in `domain.templates`.

`export` reads a bot's setup into a `BotTemplate`; `share` freezes one behind a link;
`preview` checks a template the way an import will, so what the person is shown is
what will happen. Making the bot from it is `BotManager.create_from_template`, one
layer up, because creating a bot publishes an actor.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from runtime.domain.routines import EventMatch, RoutineError, RoutineSpec
from runtime.domain.templates import BotTemplate, ImportPlan, TemplateError, TemplateRule, plan
from runtime.domain.templates import readable as readable_template
from runtime.org.bots import brief_of
from runtime.org.routines import check_routine
from runtime.persistence.repositories.bots import BotRow
from runtime.persistence.repositories.routines import RoutineRow
from runtime.persistence.repositories.templates import ShareRow
from runtime.persistence.uow import UnitOfWorkFactory

MAX_LINKS_PER_BOT = 20


class ShareRevokedError(TemplateError):
    """The link was turned off by the person who made it."""


@dataclass(frozen=True)
class Preview:
    template: BotTemplate
    plan: ImportPlan


def spec_of(row: RoutineRow) -> RoutineSpec:
    return RoutineSpec(
        name=row.name,
        instruction=row.instruction,
        kind=row.kind,
        cron=row.cron,
        timezone=row.timezone,
        source=row.source,
        match=EventMatch.model_validate(row.match or {}),
        inputs=row.inputs,
        output=row.output,
        approval=row.approval,
        when_missing=row.when_missing,
        active=row.active,
    )


def preview(template: BotTemplate, *, keep_allows: bool = False) -> Preview:
    """The template as it will be imported, or `TemplateError` saying why it cannot be."""
    template = readable_template(template)
    for routine in template.routines:
        try:
            check_routine(routine)
        except RoutineError as exc:
            raise TemplateError(f"its routine {routine.name!r} cannot run here: {exc}") from exc
    return Preview(template=template, plan=plan(template, keep_allows=keep_allows))


class TemplateService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def export(self, bot: BotRow) -> BotTemplate:
        async with self._uow() as uow:
            rules = await uow.bots.rules(bot.id)
            routines = await uow.routines.for_bot(bot.id)
        return BotTemplate(
            name=bot.name,
            label=bot.label,
            description=bot.description,
            avatar=bot.avatar,
            appearance=bot.appearance or None,
            brief=brief_of(bot),
            auto_review=bool(bot.auto_review),
            rules=[
                TemplateRule(action_type=r.action_type, host=r.host, decision=r.decision)
                for r in rules
            ],
            routines=[spec_of(r) for r in routines],
        )

    # --- links -------------------------------------------------------------------------

    async def share(self, bot: BotRow) -> ShareRow:
        """A link to the bot as it is now. Later edits are not in it: make a new one."""
        template = await self.export(bot)
        share_id = uuid.uuid4()
        async with self._uow.transaction() as uow:
            if len(await uow.template_shares.for_bot(bot.id)) >= MAX_LINKS_PER_BOT:
                raise TemplateError(
                    f"{bot.name} already has {MAX_LINKS_PER_BOT} links; turn one off first"
                )
            await uow.template_shares.create(
                share_id,
                bot.organization_id,
                bot.id,
                secrets.token_urlsafe(18),
                template.model_dump(mode="json"),
            )
            rows = await uow.template_shares.for_bot(bot.id)
        return next(r for r in rows if r.id == share_id)

    async def links(self, bot_id: uuid.UUID) -> list[ShareRow]:
        async with self._uow() as uow:
            return await uow.template_shares.for_bot(bot_id)

    async def revoke(self, bot_id: uuid.UUID, share_id: uuid.UUID) -> bool:
        async with self._uow.transaction() as uow:
            return await uow.template_shares.revoke(bot_id, share_id)

    async def shared(self, token: str) -> tuple[ShareRow, BotTemplate] | None:
        """The template behind a link; `ShareRevokedError` for one that was turned off."""
        async with self._uow() as uow:
            row = await uow.template_shares.by_token(token)
        if row is None:
            return None
        if row.revoked_at is not None:
            raise ShareRevokedError("this link was turned off by the person who shared it")
        return row, BotTemplate.model_validate(row.template)
