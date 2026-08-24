"""`apply` — one transaction, two phases, all of it or none of it.

    Phase 1 — entities
        upsert organizations, roles, connections, actors
        (reference columns nullable, left NULL)

    Phase 2 — references
        role parents; actor role, department, reportsTo
        grants → connections; policies; triggers
        then compile specs, hash, insert versions, flip active pointers

    Both phases in ONE transaction.

**Why two phases at all.** Circular references are structural, not a mistake: a
department names its head actor and that actor names its department; a role names a
parent role that may be defined later in the file. Writing entities first with their
reference columns empty, then filling them in, means the document set can be in any
order and the database is never asked to accept a pointer to a row that does not exist
yet.

**Why one transaction.** An `apply` that failed in phase 2 would leave a half-created
organization — actors with no placement, roles with no parents — and the half that
applied would look deliberate (edge case 74). There is no half-applied state, ever.
`test_m4_apply.py` kills the process mid-apply and asserts exactly that.

**The advisory lock** is taken first, on `organization_id`, and it is
transaction-scoped so a crashed applier cannot leave an organization locked (edge case
75). Two concurrent applies serialise; they do not interleave.

**One deliberate difference from `seed_department`.** M0's `publish_actor` appends a
new version *even when the spec is byte-identical*, because versions are cheap and
ambiguity is not. `apply` does not: it publishes only when the hash moved. The reason
is §13's second risk — if every apply churned a version for every actor, "nothing
changed" and "everything changed" would look the same in `actor_versions`, and §8's
guarantee would be unobservable in the one place an operator actually looks.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from runtime.budget.service import BudgetService
from runtime.budget.service import department_scope_id as budget_department_scope_id
from runtime.domain.ids import ActorId, OrganizationId, new_actor_id
from runtime.observability.logging import get_logger
from runtime.org.authority import check_escalation_acyclicity
from runtime.org.department import department_scope_id as memory_department_scope_id
from runtime.org.department import trigger_id_for
from runtime.org.governance_seed import connection_id_for, grant_id_for, policy_id_for, role_id_for
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory
from runtime.spec.compile import CompiledOrg
from runtime.spec.differ import (
    CREATE,
    DEACTIVATE,
    RENAME,
    UPDATE,
    Plan,
    build_plan,
)
from runtime.spec.errors import ApplyConflict, PlanStale

log = get_logger("spec.apply")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    organization_id: OrganizationId
    plan_id: uuid.UUID
    plan_hash: str
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    renamed: int = 0
    versions_published: dict[str, int] = field(default_factory=dict)
    """actor → the new version number, for the actors whose hash actually moved."""
    budget_changes: tuple[tuple[str, int, int], ...] = ()
    documents_recorded: int = 0

    @property
    def changed(self) -> int:
        return self.created + self.updated + self.deactivated + self.renamed


async def apply_org(
    uow_factory: UnitOfWorkFactory,
    org: CompiledOrg,
    *,
    organization_id: OrganizationId,
    plan: Plan | None = None,
    renames: dict[str, str] | None = None,
    allow_replace: bool = False,
    applied_by: str = "operator",
    source_ref: str | None = None,
    now: dt.datetime | None = None,
) -> ApplyResult:
    """Apply a compiled organization. Idempotent; safe to run twice.

    If `plan` is given, it is the diff a human read and the applier **recomputes it
    from live state and refuses if the hash moved** (edge case 76). Without one, the
    plan is computed here and applied in the same transaction, which is the right thing
    for a scripted apply and the wrong thing for one a person is reviewing — hence the
    parameter rather than a mode flag.
    """
    async with uow_factory.transaction() as uow:
        if not await uow.spec.lock_organization(organization_id):
            raise ApplyConflict(
                f"another apply holds the lock for organization {organization_id}. Two "
                "concurrent applies must serialise; wait for it to finish and re-plan, "
                "because its changes are not in your diff."
            )

        # State is read **before** the organization row is created, not after. The
        # recomputed plan has to describe the same starting point the operator's plan
        # described, and a first apply that created the org first would recompute
        # against an organization that exists — turning "create the organization" into
        # "unchanged" and making the hash comparison fail every single time.
        state = await uow.spec.load_state(organization_id)
        recomputed = build_plan(
            org,
            state,
            organization_id=organization_id,
            renames=renames or (dict(plan.renames) if plan else None),
            allow_replace=allow_replace,
            now=now,
        )
        if plan is not None:
            if plan.is_stale(now=now):
                raise PlanStale(
                    f"plan {plan.plan_id} was created at "
                    f"{plan.created_at.isoformat() if plan.created_at else '?'} and is "
                    "older than the 15-minute window. Re-run `plan`: a plan that is "
                    "right by luck is not a plan that was checked."
                )
            if plan.plan_hash() != recomputed.plan_hash():
                raise PlanStale(
                    f"the organization moved since this plan was made: plan_hash "
                    f"{plan.plan_hash()} vs {recomputed.plan_hash()} now. Somebody else "
                    "applied something. Re-run `plan` and read the new diff — applying "
                    "this one would apply a change nobody reviewed."
                )

        # Now that the diff is settled, the row every other write has a foreign key to.
        await uow.spec.ensure_organization(organization_id, org.organization)

        # A plan saved by the `plan` verb is marked applied rather than duplicated; an
        # unplanned apply records the diff it actually applied, so `apply_events.plan_id`
        # always points at something a reader can look up.
        saved = plan.plan_id if plan is not None else None
        if saved is not None and await uow.spec.get_plan(saved) is not None:
            plan_id = saved
        else:
            plan_id = uuid.uuid4()
            await uow.spec.save_plan(
                plan_id,
                organization_id,
                plan=recomputed.as_json(),
                plan_hash=recomputed.plan_hash(),
                created_by=applied_by,
            )

        result = await _write(
            uow,
            org,
            recomputed,
            organization_id=organization_id,
            applied_by=applied_by,
            source_ref=source_ref,
            plan_id=plan_id,
            now=now,
        )
        await uow.spec.mark_plan(plan_id, "APPLIED")

    log.info(
        "spec.applied",
        organization_id=str(organization_id),
        plan_hash=result.plan_hash,
        created=result.created,
        updated=result.updated,
        deactivated=result.deactivated,
        renamed=result.renamed,
        versions=result.versions_published,
    )
    return result


async def _write(
    uow: UnitOfWork,
    org: CompiledOrg,
    plan: Plan,
    *,
    organization_id: OrganizationId,
    applied_by: str,
    source_ref: str | None,
    plan_id: uuid.UUID,
    now: dt.datetime | None,
) -> ApplyResult:
    counts = {CREATE: 0, UPDATE: 0, DEACTIVATE: 0, RENAME: 0}
    for change in plan.effective:
        if change.action in counts:
            counts[change.action] += 1

    renames = dict(plan.renames)

    # --- phase 1: entities, references left NULL ------------------------------------

    for old, new in sorted(renames.items()):
        # The actor row keeps its id, its versions and its history. That is the whole
        # difference between `--rename` and the delete-plus-create the tool refuses to
        # infer (§4b).
        await uow.spec.rename_actor(organization_id, old, new)
        await uow.spec.record_event(
            organization_id,
            plan_id=plan_id,
            kind="Actor",
            name=new,
            action="rename",
            detail={"from": old},
            applied_by=applied_by,
        )

    # M5. Departments are rows now (migration 034), and they are written **first** in
    # phase 1 — before roles and actors, both of which carry a `department` the trigger
    # resolves to a foreign key. A role written before its department gets a NULL
    # `department_id` and stays wrong until the next apply, which is the class of bug
    # that makes a config plane untrustworthy: it applies cleanly and is subtly wrong.
    #
    # `parent_id` is deferred to phase 2 like every other reference (M4's second
    # deviation), so a department tree that is cyclic fails in `validation` with a
    # message naming the two documents rather than in a topological sort naming
    # seventeen.
    for department in org.departments:
        await uow.departments.ensure(
            budget_department_scope_id(organization_id, department.name),
            organization_id,
            department.name,
            memory_scope_id=memory_department_scope_id(organization_id, department.name),
            parent_id=None,  # phase 2
            head_actor_name=department.head,
            description=department.description,
        )

    for role in org.roles:
        await uow.authority.upsert_role(
            role_id_for(organization_id, role.name),
            organization_id,
            name=role.name,
            department=role.department,
            parent_role_id=None,  # phase 2
            rank=role.rank,
            approver=role.approver,
            approver_daily_budget=role.approver_daily_budget,
            base_authority=dict(role.base_authority),
        )

    for connection in org.connections:
        await uow.authority.upsert_connection(
            connection_id_for(organization_id, connection.name),
            organization_id,
            name=connection.name,
            provider=connection.provider,
            scopes=list(connection.scopes),
            credential_name=connection.credential_name,
            status=connection.status,
        )

    existing_actors = {
        name: ActorId(actor_id)
        for name, actor_id in (await uow.spec.actor_ids(organization_id)).items()
    }
    actor_ids: dict[str, ActorId] = {}
    for actor in org.actors:
        actor_id = existing_actors.get(actor.name)
        if actor_id is None:
            actor_id = new_actor_id()
            await uow.actors.create_actor(
                actor_id, organization_id, actor.name, actor.spec.kind.value
            )
        actor_ids[actor.name] = actor_id

    # --- phase 2: references ---------------------------------------------------------

    for role in org.roles:
        if role.parent is None:
            continue
        await uow.spec.set_role_parent(
            organization_id, role.name, role_id_for(organization_id, role.parent)
        )

    for department in org.departments:
        if department.parent is None:
            continue
        await uow.departments.ensure(
            budget_department_scope_id(organization_id, department.name),
            organization_id,
            department.name,
            memory_scope_id=memory_department_scope_id(organization_id, department.name),
            parent_id=budget_department_scope_id(organization_id, department.parent),
            head_actor_name=department.head,
            description=department.description,
        )
    if org.departments:
        # Edge case 78's rule, applied to departments. Only when the document set names
        # at least one: an apply of a document set with no `Department` documents at all
        # is not a statement that the organization has no departments, and deactivating
        # every one of them on the strength of an omission would be the config plane
        # doing something nobody asked for.
        await uow.departments.deactivate_missing(organization_id, [d.name for d in org.departments])

    for actor in org.actors:
        await uow.authority.set_actor_role(
            organization_id,
            actor.name,
            role_name=actor.role,
            department=actor.department,
        )
        await uow.spec.set_actor_identity(
            organization_id, actor.name, reports_to=actor.reports_to, uid=actor.uid
        )
        # M5. The limits M4 validated and stored but never enforced. On the actor row
        # rather than in `ActorSpec`, so `spec_hash` is untouched and M4 §8's
        # round-trip stays green — the same route `MemoryProfile` did not take because
        # nothing yet read it, and this one is read at every admission.
        await uow.actors.set_delegation(
            organization_id,
            actor.name,
            actor.delegation.model_dump(mode="json") if actor.delegation else None,
        )

    for policy in org.policies:
        await uow.authority.upsert_policy(
            policy_id_for(organization_id, policy.scope_type, policy.scope_id, policy.action),
            organization_id,
            scope_type=policy.scope_type,
            scope_id=policy.scope_id,
            action=policy.action,
            level=policy.level,
            approver_role=policy.approver_role,
            max_escalations=policy.max_escalations,
            on_expiry=policy.on_expiry,
            ttl_seconds=policy.ttl_seconds,
        )

    for grant in org.grants:
        await uow.authority.grant_tool(
            grant_id_for(organization_id, grant.subject_type, grant.subject_id, grant.tool),
            organization_id,
            subject_type=grant.subject_type,
            subject_id=grant.subject_id,
            tool=grant.tool,
            connection_id=(
                connection_id_for(organization_id, grant.connection) if grant.connection else None
            ),
            granted_by=applied_by,
        )

    for trigger in org.triggers:
        await uow.triggers.upsert(
            trigger_id_for(organization_id, trigger.key),
            organization_id=organization_id,
            key=trigger.key,
            actor_name=trigger.actor,
            cron=trigger.cron,
            timezone=trigger.timezone,
            payload=dict(trigger.input),
            catchup_policy=trigger.catchup,
        )

    # --- removals: deactivate and revoke, never delete --------------------------------

    for change in plan.effective:
        if change.action != DEACTIVATE:
            continue
        if change.kind == "Actor":
            await uow.spec.deactivate_actor(organization_id, change.name)
        elif change.kind == "Trigger":
            await uow.spec.deactivate_trigger(organization_id, change.name)
        elif change.kind == "ToolGrant":
            subject, _, tool = change.name.partition("/")
            subject_type, _, subject_id = subject.partition(":")
            await uow.authority.revoke_tool(
                organization_id,
                subject_type=subject_type,
                subject_id=subject_id,
                tool=tool,
                revoked_by=applied_by,
            )

    # --- the §3 compile-time checks, inside the transaction ---------------------------
    # Same placement, and the same argument, as `seed_governance`: a policy set that
    # fails them is not merely reported, it is *not written*. A half-applied cyclic
    # policy is worse than none, because the half that applied looks deliberate.
    check_escalation_acyclicity(
        await uow.authority.roles(organization_id),
        await uow.authority.policies(organization_id),
    )

    # --- versions ---------------------------------------------------------------------

    live_hashes = await uow.spec.active_spec_hashes(organization_id)
    published: dict[str, int] = {}
    for actor in org.actors:
        if live_hashes.get(actor.name) == actor.spec_hash:
            continue
        next_version = await uow.spec.next_actor_version(actor_ids[actor.name])
        version_id = await uow.actors.add_version(
            actor_ids[actor.name],
            next_version,
            actor.spec.model_dump(mode="json"),
            actor.spec_hash,
        )
        await uow.actors.set_active_version(actor_ids[actor.name], version_id)
        published[actor.name] = next_version

    # --- budget ------------------------------------------------------------------------

    budget_changes: tuple[tuple[str, int, int], ...] = ()
    if org.budget is not None:
        department_of = {a.name: a.department for a in org.actors}
        budget_changes = tuple(
            await BudgetService().apply_limits(
                uow,
                organization_id,
                organization_cents=org.budget.organization,
                departments=org.budget.departments,
                actors={
                    actor_ids[name]: (department_of.get(name), cents)
                    for name, cents in org.budget.actors.items()
                    if name in actor_ids
                },
                now=now,
            )
        )

    # --- the documents, and one event per change ---------------------------------------

    recorded = 0
    for document in org.documents:
        if await uow.spec.record_document(
            organization_id,
            kind=document.kind.value,
            name=document.name,
            source_yaml=document.source_yaml,
            compiled_spec=document.compiled,
            spec_hash=document.spec_hash,
            source_ref=source_ref,
            applied_by=applied_by,
        ):
            recorded += 1

    for change in plan.effective:
        if change.action == RENAME:
            continue  # already recorded in phase 1, before the row moved
        await uow.spec.record_event(
            organization_id,
            plan_id=plan_id,
            kind=change.kind,
            name=change.name,
            action=change.action,
            detail=change.detail,
            applied_by=applied_by,
        )

    return ApplyResult(
        organization_id=organization_id,
        plan_id=plan_id,
        plan_hash=plan.plan_hash(),
        created=counts[CREATE],
        updated=counts[UPDATE],
        deactivated=counts[DEACTIVATE],
        renamed=counts[RENAME],
        versions_published=published,
        budget_changes=budget_changes,
        documents_recorded=recorded,
    )


__all__ = ["ApplyResult", "apply_org"]
