"""Bringing the department into existence.

One function, idempotent, safe to call on every boot. It publishes the four actor
specs, seeds the goal and project, and installs the four cron triggers.

Idempotent does not mean "does nothing the second time". Publishing an actor
appends a new immutable version and flips the active pointer even when the spec is
byte-identical, which is deliberate M0 behaviour: versions are cheap, and silently
reusing the previous version when the spec happens to match makes "which version was
this run admitted under" ambiguous at exactly the moment somebody is trying to
answer it. `seed_department(republish=False)` is the flag for a boot that should not
create a new version.

The order matters in two places now. Triggers are installed **last**: a trigger whose
actor does not exist yet will fire, fail to resolve the actor, and leave a
`trigger_fires` row with no run — which is a diagnosable state, but a needless one.

And governance is installed **after the actors and before the triggers**. After,
because a role placement is an UPDATE on an `actors` row that has to exist; before,
because a cron that fires into an ungoverned department would admit runs whose frozen
authority says nothing is gated. M2's whole point is that the second window does not
exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.ids import ActorId, GoalId, OrganizationId, ProjectId, TriggerId
from runtime.gateway.builtin import default_action_floors
from runtime.graphs.department import assert_registered
from runtime.observability.logging import get_logger
from runtime.org.agents_config import AgentsConfig, load_agents_config
from runtime.org.department import (
    DEPARTMENT_SPECS,
    GOAL_NAME,
    GOAL_STATEMENT,
    PROJECT_DESCRIPTION,
    PROJECT_NAME,
    goal_id_for,
    project_id_for,
)
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.governance_boot import GovernanceSeeded, seed_governance
from runtime.runtime.scheduler import install_triggers
from runtime.settings import Settings, get_settings

log = get_logger("runtime.department")


@dataclass(frozen=True, slots=True)
class Department:
    organization_id: OrganizationId
    goal_id: GoalId
    project_id: ProjectId
    actors: dict[str, ActorId]
    triggers: list[TriggerId]
    governance: GovernanceSeeded | None = None


async def seed_department(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    *,
    organization_name: str = "acme",
    republish: bool = True,
    with_triggers: bool = True,
    with_governance: bool = True,
    settings: Settings | None = None,
    agents_config: AgentsConfig | None = None,
) -> Department:
    """Create everything the weekly loop needs. Safe to run repeatedly."""
    # Fail here rather than when a cron fires days from now. A worker that starts
    # without a graph an actor's spec names does not find out until the Monday
    # plan, and by then it looks like a scheduling problem.
    assert_registered()

    # Before the organization row, not after. A malformed agents YAML should fail a
    # deploy having written nothing, rather than half-seed a department and leave the
    # operator to work out which actors got the new profiles and which kept the old.
    resolved = settings or get_settings()
    config = agents_config or load_agents_config(resolved.agents_config_path)
    specs = config.apply(DEPARTMENT_SPECS)

    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, organization_name)

    actors: dict[str, ActorId] = {}
    for spec in specs:
        if not republish:
            async with uow_factory() as uow:
                try:
                    existing = await uow.actors.resolve_active(organization_id, spec.name)
                except Exception:
                    existing = None
            if existing is not None:
                actors[spec.name] = existing.actor_id
                continue
        registered = await registrar.publish_actor(organization_id, spec)
        actors[spec.name] = registered.actor_id
        log.info(
            "department.actor_published",
            actor=spec.name,
            kind=spec.kind.value,
            version=registered.version,
        )

    goal_id = goal_id_for(organization_id)
    project_id = project_id_for(organization_id)
    async with uow_factory.transaction() as uow:
        goal_id = await uow.goals.ensure_goal(goal_id, organization_id, GOAL_NAME, GOAL_STATEMENT)
        project_id = await uow.goals.ensure_project(
            project_id, organization_id, goal_id, PROJECT_NAME, PROJECT_DESCRIPTION
        )

    governance = (
        await seed_governance(uow_factory, organization_id, action_floors=default_action_floors())
        if with_governance
        else None
    )

    triggers = await install_triggers(uow_factory, organization_id) if with_triggers else []

    log.info(
        "department.seeded",
        organization_id=str(organization_id),
        actors=sorted(actors),
        triggers=len(triggers),
        gated=governance.gated_actions if governance else None,
    )
    return Department(
        organization_id=organization_id,
        goal_id=goal_id,
        project_id=project_id,
        actors=actors,
        triggers=triggers,
        governance=governance,
    )
