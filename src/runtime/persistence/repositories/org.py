"""Goals, projects, and the metric views.

The metric queries live here rather than in the org service for one reason: the
views in migration 015 are the definition of the four numbers, and the only Python
that should touch them is a thin read. Any arithmetic done here instead of in SQL
would be a second definition of a metric, and two definitions of a metric is how a
dashboard and a report come to disagree in a meeting.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.ids import GoalId, ProjectId


@dataclass(frozen=True, slots=True)
class GoalRow:
    id: GoalId
    name: str
    statement: str
    horizon: str


@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: ProjectId
    goal_id: GoalId
    name: str
    description: str


class GoalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def ensure_goal(
        self,
        goal_id: GoalId,
        organization_id: uuid.UUID,
        name: str,
        statement: str,
        horizon: str = "quarter",
    ) -> GoalId:
        await self._s.execute(
            text(
                """
                INSERT INTO goals (id, organization_id, name, statement, horizon)
                VALUES (:id, :org, :name, :statement, :horizon)
                ON CONFLICT ON CONSTRAINT uq_goal_name
                    DO UPDATE SET statement = EXCLUDED.statement
                """
            ),
            {
                "id": goal_id,
                "org": organization_id,
                "name": name,
                "statement": statement,
                "horizon": horizon,
            },
        )
        found = (
            await self._s.execute(
                text("SELECT id FROM goals WHERE organization_id = :org AND name = :name"),
                {"org": organization_id, "name": name},
            )
        ).scalar_one()
        return GoalId(found)

    async def ensure_project(
        self,
        project_id: ProjectId,
        organization_id: uuid.UUID,
        goal_id: GoalId,
        name: str,
        description: str = "",
    ) -> ProjectId:
        await self._s.execute(
            text(
                """
                INSERT INTO projects (id, organization_id, goal_id, name, description)
                VALUES (:id, :org, :goal, :name, :desc)
                ON CONFLICT ON CONSTRAINT uq_project_name
                    DO UPDATE SET description = EXCLUDED.description
                """
            ),
            {
                "id": project_id,
                "org": organization_id,
                "goal": goal_id,
                "name": name,
                "desc": description,
            },
        )
        found = (
            await self._s.execute(
                text("SELECT id FROM projects WHERE organization_id = :org AND name = :name"),
                {"org": organization_id, "name": name},
            )
        ).scalar_one()
        return ProjectId(found)

    async def active_goal(self, organization_id: uuid.UUID) -> GoalRow | None:
        r = (
            await self._s.execute(
                text(
                    """
                    SELECT id, name, statement, horizon FROM goals
                     WHERE organization_id = :org AND active ORDER BY created_at LIMIT 1
                    """
                ),
                {"org": organization_id},
            )
        ).one_or_none()
        if r is None:
            return None
        return GoalRow(id=GoalId(r.id), name=r.name, statement=r.statement, horizon=r.horizon)

    async def active_project(self, organization_id: uuid.UUID) -> ProjectRow | None:
        r = (
            await self._s.execute(
                text(
                    """
                    SELECT id, goal_id, name, description FROM projects
                     WHERE organization_id = :org AND active ORDER BY created_at LIMIT 1
                    """
                ),
                {"org": organization_id},
            )
        ).one_or_none()
        if r is None:
            return None
        return ProjectRow(
            id=ProjectId(r.id), goal_id=GoalId(r.goal_id), name=r.name, description=r.description
        )


class MetricsRepository:
    """Reads of the migration-015 views. No arithmetic — the views are the truth."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def weekly(
        self, view: str, organization_id: uuid.UUID, week_start: dt.date | None = None
    ) -> list[dict[str, Any]]:
        if view not in _ALLOWED_VIEWS:
            raise ValueError(f"{view!r} is not a metric view; known: {sorted(_ALLOWED_VIEWS)}")
        clause = " AND week_start = :week" if week_start else ""
        params: dict[str, Any] = {"org": organization_id}
        if week_start:
            params["week"] = week_start
        rows = (
            await self._s.execute(
                text(
                    f"SELECT * FROM {view} WHERE organization_id = :org{clause} ORDER BY week_start"
                ),
                params,
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def task_facts(
        self, organization_id: uuid.UUID, since: dt.date | None = None
    ) -> list[dict[str, Any]]:
        clause = " AND week_start >= :since" if since else ""
        params: dict[str, Any] = {"org": organization_id}
        if since:
            params["since"] = since
        rows = (
            await self._s.execute(
                text(f"SELECT * FROM v_task_facts WHERE organization_id = :org{clause}"),
                params,
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def spend_by_work_class(
        self, organization_id: uuid.UUID, since: dt.datetime
    ) -> dict[str, int]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT COALESCE(work_class, '(none)') AS wc, sum(cost_cents) AS cents
                      FROM usage_ledger
                     WHERE organization_id = :org AND created_at >= :since
                     GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"org": organization_id, "since": since},
            )
        ).all()
        return {r.wc: int(r.cents) for r in rows}

    async def top_calls_by_cost(
        self, organization_id: uuid.UUID, work_class: str, since: dt.datetime, limit: int = 3
    ) -> list[dict[str, Any]]:
        """The §10 coordination-ratio diagnosis, as a query rather than a grep."""
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT call_site, count(*) AS calls, sum(cost_cents) AS cents,
                           sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok
                      FROM usage_ledger
                     WHERE organization_id = :org AND work_class = :wc AND created_at >= :since
                     GROUP BY call_site ORDER BY cents DESC LIMIT :limit
                    """
                ),
                {"org": organization_id, "wc": work_class, "since": since, "limit": limit},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def total_spend_cents(self, organization_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT COALESCE(sum(cost_cents), 0) FROM usage_ledger "
                        "WHERE organization_id = :org"
                    ),
                    {"org": organization_id},
                )
            ).scalar_one()
        )


_ALLOWED_VIEWS = frozenset(
    {
        "v_task_facts",
        "v_weekly_spend",
        "v_metric_cost_per_accepted",
        "v_metric_rejection_rate",
        "v_metric_unassisted_completion",
        "v_metric_coordination_ratio",
        "v_metric_dashboard",
    }
)
"""An allow-list because the view name is interpolated into the statement. The
alternative is seven near-identical methods; this is one method and a closed set."""
