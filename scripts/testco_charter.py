"""Give TestCo something to work on.

`spec apply` writes the org chart — actors, roles, grants, budgets, triggers — but a
charter is not org structure, so it is not in `config/`. The head graph plans against
whatever `goals.charter(org)` returns, so one goal and one project is the whole thing.

    uv run python scripts/testco_charter.py <organization-id>
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from runtime.domain.ids import GoalId, OrganizationId, ProjectId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import get_settings

GOAL_NAME = "q3-developer-awareness"
GOAL_STATEMENT = (
    "Make TestCo's durable-execution runtime known to the engineers who would use it: "
    "know who else claims exactly-once agent execution and what they actually deliver, "
    "publish one substantive piece a week aimed at a gap, and report weekly on whether "
    "any of it moved."
)
PROJECT_NAME = "weekly-growth-loop"
PROJECT_DESCRIPTION = "Monday to Friday: plan, research, draft, gate, measure, summarise."


async def main(organization_id: OrganizationId) -> None:
    factory = UnitOfWorkFactory(get_settings())
    goal_id = GoalId(uuid.uuid5(uuid.NAMESPACE_URL, f"goal:{organization_id}:{GOAL_NAME}"))
    project_id = ProjectId(
        uuid.uuid5(uuid.NAMESPACE_URL, f"project:{organization_id}:{PROJECT_NAME}")
    )
    async with factory.transaction() as uow:
        goal_id = await uow.goals.ensure_goal(
            goal_id, organization_id, GOAL_NAME, GOAL_STATEMENT
        )
        project_id = await uow.goals.ensure_project(
            project_id, organization_id, goal_id, PROJECT_NAME, PROJECT_DESCRIPTION
        )
    print(f"goal    {goal_id}  {GOAL_NAME}")
    print(f"project {project_id}  {PROJECT_NAME}")


if __name__ == "__main__":
    asyncio.run(main(OrganizationId(uuid.UUID(sys.argv[1]))))
