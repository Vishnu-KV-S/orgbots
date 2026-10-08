"""The skills library and demonstrations. See migration 044."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = """
    id, organization_id, name, title, when_to_use, inputs, steps, checks, output, approvals,
    status, source, source_bot_id, recording_id, version, updated_by_kind, updated_by_name,
    use_count, last_used_at, created_at, updated_at
"""

_RECORDING_COLUMNS = (
    "id, organization_id, bot_id, goal, status, steps, skill_id, started_at, stopped_at"
)

BODY = ("title", "when_to_use", "inputs", "steps", "checks", "output", "approvals")
EDITABLE = frozenset({*BODY, "name", "status"})


@dataclass(frozen=True, slots=True)
class SkillRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    title: str
    when_to_use: str
    inputs: str
    steps: list[str]
    checks: str
    output: str
    approvals: str
    status: str
    source: str
    source_bot_id: uuid.UUID | None
    recording_id: uuid.UUID | None
    version: int
    updated_by_kind: str
    updated_by_name: str
    use_count: int
    last_used_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def body(self) -> dict[str, Any]:
        """The content, keyed as `domain.skills.SkillBody` names it."""
        return {
            "title": self.title,
            "when": self.when_to_use,
            "inputs": self.inputs,
            "steps": list(self.steps),
            "checks": self.checks,
            "output": self.output,
            "approvals": self.approvals,
        }


@dataclass(frozen=True, slots=True)
class RecordingRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    bot_id: uuid.UUID
    goal: str
    status: str
    steps: list[dict[str, Any]]
    skill_id: uuid.UUID | None
    started_at: dt.datetime
    stopped_at: dt.datetime | None


def _skill(r: Any) -> SkillRow:
    return SkillRow(
        id=r.id,
        organization_id=r.organization_id,
        name=r.name,
        title=r.title,
        when_to_use=r.when_to_use,
        inputs=r.inputs,
        steps=[str(s) for s in (r.steps or [])],
        checks=r.checks,
        output=r.output,
        approvals=r.approvals,
        status=r.status,
        source=r.source,
        source_bot_id=r.source_bot_id,
        recording_id=r.recording_id,
        version=r.version,
        updated_by_kind=r.updated_by_kind,
        updated_by_name=r.updated_by_name,
        use_count=r.use_count,
        last_used_at=r.last_used_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _recording(r: Any) -> RecordingRow:
    return RecordingRow(
        id=r.id,
        organization_id=r.organization_id,
        bot_id=r.bot_id,
        goal=r.goal,
        status=r.status,
        steps=list(r.steps or []),
        skill_id=r.skill_id,
        started_at=r.started_at,
        stopped_at=r.stopped_at,
    )


class SkillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- skills ------------------------------------------------------------------------

    async def create(
        self,
        skill_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        name: str,
        body: dict[str, Any],
        status: str,
        source: str,
        source_bot_id: uuid.UUID | None = None,
        recording_id: uuid.UUID | None = None,
        updated_by_kind: str = "person",
        updated_by_name: str = "",
    ) -> bool:
        """False when this id already exists (a replayed step)."""
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_skills (
                    id, organization_id, name, title, when_to_use, inputs, steps, checks,
                    output, approvals, status, source, source_bot_id, recording_id,
                    updated_by_kind, updated_by_name
                ) VALUES (
                    :id, :org, :name, :title, :when, :inputs, CAST(:steps AS jsonb), :checks,
                    :output, :approvals, :status, :source, :bot, :recording, :by_kind, :by_name
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": skill_id,
                "org": organization_id,
                "name": name,
                "title": body.get("title", ""),
                "when": body.get("when", ""),
                "inputs": body.get("inputs", ""),
                "steps": json.dumps(list(body.get("steps") or [])),
                "checks": body.get("checks", ""),
                "output": body.get("output", ""),
                "approvals": body.get("approvals", ""),
                "status": status,
                "source": source,
                "bot": source_bot_id,
                "recording": recording_id,
                "by_kind": updated_by_kind,
                "by_name": updated_by_name,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def get(self, skill_id: uuid.UUID) -> SkillRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM bot_skills WHERE id = :id AND deleted_at IS NULL"),
                {"id": skill_id},
            )
        ).first()
        return None if row is None else _skill(row)

    async def by_name(self, organization_id: uuid.UUID, name: str) -> SkillRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_skills WHERE organization_id = :org "
                    "AND lower(name) = lower(:name) AND deleted_at IS NULL"
                ),
                {"org": organization_id, "name": name.strip().lstrip("/")},
            )
        ).first()
        return None if row is None else _skill(row)

    async def library(self, organization_id: uuid.UUID) -> list[SkillRow]:
        """Every live skill, most used first, then newest."""
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_skills WHERE organization_id = :org "
                    "AND deleted_at IS NULL ORDER BY use_count DESC, updated_at DESC"
                ),
                {"org": organization_id},
            )
        ).all()
        return [_skill(r) for r in rows]

    async def count(self, organization_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT count(*) FROM bot_skills WHERE organization_id = :org "
                        "AND deleted_at IS NULL"
                    ),
                    {"org": organization_id},
                )
            ).scalar_one()
        )

    async def update(
        self, skill_id: uuid.UUID, fields: dict[str, Any], *, by_kind: str, by_name: str
    ) -> None:
        """Change fields and count a version. `fields` uses column names."""
        unknown = set(fields) - EDITABLE
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(
            f"{name} = CAST(:{name} AS jsonb)" if name == "steps" else f"{name} = :{name}"
            for name in sorted(fields)
        )
        params = {
            name: json.dumps(value) if name == "steps" else value for name, value in fields.items()
        }
        await self._s.execute(
            text(
                f"UPDATE bot_skills SET {assignments}, version = version + 1, "
                "updated_by_kind = :by_kind, updated_by_name = :by_name, updated_at = now() "
                "WHERE id = :id"
            ),
            {**params, "id": skill_id, "by_kind": by_kind, "by_name": by_name},
        )

    async def used(self, skill_ids: list[uuid.UUID]) -> None:
        if not skill_ids:
            return
        await self._s.execute(
            text(
                "UPDATE bot_skills SET use_count = use_count + 1, last_used_at = now() "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": skill_ids},
        )

    async def soft_delete(self, skill_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bot_skills SET deleted_at = now() WHERE id = :id"), {"id": skill_id}
        )

    # --- demonstrations -------------------------------------------------------------------

    async def start_recording(
        self, recording_id: uuid.UUID, organization_id: uuid.UUID, bot_id: uuid.UUID, goal: str
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO bot_recordings (id, organization_id, bot_id, goal) "
                "VALUES (:id, :org, :bot, :goal)"
            ),
            {"id": recording_id, "org": organization_id, "bot": bot_id, "goal": goal},
        )

    async def live_recording(self, bot_id: uuid.UUID) -> RecordingRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_RECORDING_COLUMNS} FROM bot_recordings WHERE bot_id = :bot "
                    "AND status = 'recording'"
                ),
                {"bot": bot_id},
            )
        ).first()
        return None if row is None else _recording(row)

    async def get_recording(self, recording_id: uuid.UUID) -> RecordingRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_RECORDING_COLUMNS} FROM bot_recordings WHERE id = :id"),
                {"id": recording_id},
            )
        ).first()
        return None if row is None else _recording(row)

    async def finish_recording(
        self, recording_id: uuid.UUID, *, status: str, steps: list[dict[str, Any]]
    ) -> bool:
        """Conditional on still recording, so a double Stop finishes it once."""
        result = await self._s.execute(
            text(
                "UPDATE bot_recordings SET status = :status, steps = CAST(:steps AS jsonb), "
                "stopped_at = now() WHERE id = :id AND status = 'recording'"
            ),
            {"id": recording_id, "status": status, "steps": json.dumps(steps, default=str)},
        )
        return bool(getattr(result, "rowcount", 0))

    async def link_recording(self, recording_id: uuid.UUID, skill_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE bot_recordings SET skill_id = :skill WHERE id = :id"),
            {"id": recording_id, "skill": skill_id},
        )
