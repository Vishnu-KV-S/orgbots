"""Goals and projects — reads, and the one-time seed.

M1 runs a single goal and a single project, so this service is almost entirely
`ensure` and two getters. It exists at all because the planning node needs the goal
statement, and a graph reaching into a repository through another service's private
unit-of-work factory is the kind of shortcut that is invisible until the day
somebody changes the factory.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.ids import OrganizationId
from runtime.persistence.repositories.org import GoalRow, ProjectRow
from runtime.persistence.uow import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class Charter:
    """What the department is for, as one value the planner can read."""

    goal: GoalRow | None
    project: ProjectRow | None

    @property
    def statement(self) -> str | None:
        return self.goal.statement if self.goal else None


class GoalService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def charter(self, organization_id: OrganizationId) -> Charter:
        async with self._uow() as uow:
            return Charter(
                goal=await uow.goals.active_goal(organization_id),
                project=await uow.goals.active_project(organization_id),
            )
