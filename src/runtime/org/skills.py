"""The skills library, as the graph and the API see it.

One library per organization (`domain.skills`). A person writes, edits, reviews and
deletes skills through `/v1/skills`; a bot saves one with `save_skill` and loads one
with `use_skill`, or gets it loaded for it when its person names it as `/name`. All of
it goes through `SkillService`, so a skill a bot saves is held to the same name rules
and the same size limits as one a person types.

What a bot may save is the graph's to decide (only on its person's own turn); a
demonstration's write-up is saved as a `draft`, and stays out of every bot's index
until a person marks it ready.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from runtime.domain.skills import (
    MAX_SKILLS,
    SkillBody,
    SkillDraft,
    SkillError,
    check_name,
    mentioned,
)
from runtime.persistence.repositories.skills import SkillRow
from runtime.persistence.uow import UnitOfWorkFactory

_COLUMN = {"when": "when_to_use"}


def _columns(body: dict[str, Any]) -> dict[str, Any]:
    return {_COLUMN.get(k, k): v for k, v in body.items()}


@dataclass(frozen=True, slots=True)
class SavedSkill:
    skill: SkillRow
    outcome: str
    """`created`, `updated` or `unchanged`."""


def skill_id_for(run_id: object, step: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botskill:{run_id}:{step}")


class SkillService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def library(self, organization_id: uuid.UUID) -> list[SkillRow]:
        async with self._uow() as uow:
            return await uow.skills.library(organization_id)

    async def index(self, organization_id: uuid.UUID) -> list[tuple[str, str, str]]:
        """Ready skills as a bot's prompt lists them: `(name, title, when)`."""
        return [
            (s.name, s.title, s.when_to_use)
            for s in await self.library(organization_id)
            if s.status == "ready"
        ]

    async def get(self, skill_id: uuid.UUID) -> SkillRow | None:
        async with self._uow() as uow:
            return await uow.skills.get(skill_id)

    async def by_name(self, organization_id: uuid.UUID, name: str) -> SkillRow | None:
        async with self._uow() as uow:
            return await uow.skills.by_name(organization_id, name)

    async def mentioned_in(self, organization_id: uuid.UUID, text: str) -> list[SkillRow]:
        """The skills a message names as `/name`, drafts included — a person who names a
        draft has chosen it."""
        names = mentioned(text)
        if not names:
            return []
        async with self._uow() as uow:
            found = [await uow.skills.by_name(organization_id, n) for n in names]
        return [s for s in found if s is not None]

    async def used(self, skills: list[SkillRow]) -> None:
        async with self._uow.transaction() as uow:
            await uow.skills.used([s.id for s in skills])

    # --- a person's -----------------------------------------------------------------

    async def create(
        self,
        organization_id: uuid.UUID,
        name: str,
        body: SkillBody,
        *,
        status: str = "ready",
        source: str = "person",
        skill_id: uuid.UUID | None = None,
        source_bot_id: uuid.UUID | None = None,
        recording_id: uuid.UUID | None = None,
        by_kind: str = "person",
        by_name: str = "",
    ) -> SkillRow:
        name = check_name(name)
        if not body.steps:
            raise SkillError("a skill needs at least one step")
        sid = skill_id or uuid.uuid4()
        async with self._uow.transaction() as uow:
            existing = await uow.skills.get(sid)
            if existing is not None:
                return existing
            if await uow.skills.by_name(organization_id, name) is not None:
                raise SkillError(f"there is already a skill called /{name}")
            if await uow.skills.count(organization_id) >= MAX_SKILLS:
                raise SkillError(f"the library is full ({MAX_SKILLS} skills); delete one first")
            await uow.skills.create(
                sid,
                organization_id,
                name=name,
                body=body.model_dump(),
                status=status,
                source=source,
                source_bot_id=source_bot_id,
                recording_id=recording_id,
                updated_by_kind=by_kind,
                updated_by_name=by_name,
            )
            if recording_id is not None:
                await uow.skills.link_recording(recording_id, sid)
            row = await uow.skills.get(sid)
        assert row is not None
        return row

    async def update(
        self,
        skill: SkillRow,
        *,
        name: str | None = None,
        body: dict[str, Any] | None = None,
        status: str | None = None,
        by_kind: str = "person",
        by_name: str = "",
    ) -> SkillRow:
        fields: dict[str, Any] = {}
        if name is not None and check_name(name) != skill.name:
            fields["name"] = check_name(name)
        if body:
            merged = {**skill.body(), **body}
            checked = SkillBody.model_validate(merged)
            if not checked.steps:
                raise SkillError("a skill needs at least one step")
            fields.update(
                {
                    k: v
                    for k, v in _columns(checked.model_dump()).items()
                    if getattr(skill, k) != v
                }
            )
        if status is not None and status != skill.status:
            fields["status"] = status
        if not fields:
            return skill
        async with self._uow.transaction() as uow:
            if "name" in fields:
                clash = await uow.skills.by_name(skill.organization_id, fields["name"])
                if clash is not None and clash.id != skill.id:
                    raise SkillError(f"there is already a skill called /{fields['name']}")
            await uow.skills.update(skill.id, fields, by_kind=by_kind, by_name=by_name)
            row = await uow.skills.get(skill.id)
        assert row is not None
        return row

    async def delete(self, skill_id: uuid.UUID) -> None:
        async with self._uow.transaction() as uow:
            await uow.skills.soft_delete(skill_id)

    # --- a bot's ----------------------------------------------------------------------

    async def save_draft(
        self,
        bot: Any,
        draft: SkillDraft,
        *,
        run_id: object,
        step: int,
        recording_id: uuid.UUID | None = None,
    ) -> SavedSkill:
        """`save_skill`: create by name, or change the named skill.

        A write-up of a demonstration (`recording_id`) is created as a draft; one the
        person asked for in the conversation is ready. A bot's change to an existing
        skill keeps its status — it does not promote a draft nobody has read.
        """
        name = check_name(draft.name)
        current = await self.by_name(bot.organization_id, name)
        changes = draft.body_changes()
        if current is None:
            row = await self.create(
                bot.organization_id,
                name,
                SkillBody.model_validate(changes),
                status="draft" if recording_id else "ready",
                source="demonstration" if recording_id else "bot",
                skill_id=skill_id_for(run_id, step),
                source_bot_id=bot.id,
                recording_id=recording_id,
                by_kind="bot",
                by_name=bot.name,
            )
            return SavedSkill(row, "created")
        updated = await self.update(current, body=changes, by_kind="bot", by_name=bot.name)
        return SavedSkill(updated, "unchanged" if updated is current else "updated")
