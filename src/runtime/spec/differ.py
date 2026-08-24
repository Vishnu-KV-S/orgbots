"""The diff, and the plan it produces.

A `plan` is a *reviewable* artifact. M4 §13's first risk is a quiet bad apply — no
crash, just an actor with different permissions — and the stated guard is *"treat a
plan diff like a code review"*. Everything about this module follows from that:

**Canonical ordering, always** (edge case 85). Changes are sorted by kind and then by
name; per-change details are sorted by field. Two people running `plan` against the
same state get byte-identical output, so a diff between two plans means something.

**Nothing is deleted.** An actor removed from the documents is *deactivated* (edge case
78); a grant removed is *revoked*; a trigger removed is deactivated. Rows in the
database that the documents do not mention and that have no deactivate semantics — a
role, a connection — are reported as warnings rather than removed, because `apply` is
not a reconciler (§2) and a control plane that deletes things it does not understand is
one you cannot point at half an organization.

**`plan_hash` covers the changes and nothing else.** Not the timestamp, not who ran it,
not the file paths. It is the answer to "is this still the diff I read?" and anything in
it that can move without the meaning moving would make it answer a different question
(edge case 76).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import OrganizationId
from runtime.persistence.repositories.spec import OrgState
from runtime.spec.compile import CompiledOrg
from runtime.spec.errors import RenameRefused, SpecValidationError

STALE_AFTER_SECONDS = 15 * 60
"""`[CHOSEN]` §9. A plan older than this is `STALE` regardless of whether state moved.
A plan that is right by luck is not a plan that was checked."""

CREATE = "create"
UPDATE = "update"
DEACTIVATE = "deactivate"
UNCHANGED = "unchanged"
RENAME = "rename"


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    name: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    """`field → [before, after]`, or whatever the action needs. Sorted on the way in."""

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "action": self.action,
            "detail": {k: self.detail[k] for k in sorted(self.detail)},
        }

    def render(self) -> str:
        marker = {CREATE: "+", UPDATE: "~", DEACTIVATE: "-", RENAME: "»", UNCHANGED: " "}[
            self.action
        ]
        head = f"{marker} {self.kind}/{self.name}"
        if self.action is UNCHANGED or not self.detail:
            return head
        lines = [head]
        for key in sorted(self.detail):
            value = self.detail[key]
            if isinstance(value, list | tuple) and len(value) == 2:
                lines.append(f"    {key}: {value[0]!r} -> {value[1]!r}")
            else:
                lines.append(f"    {key}: {value!r}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Plan:
    organization: str
    organization_id: UUID
    changes: tuple[Change, ...] = ()
    warnings: tuple[str, ...] = ()
    renames: tuple[tuple[str, str], ...] = ()
    created_at: dt.datetime | None = None
    plan_id: UUID | None = None

    @property
    def effective(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.action != UNCHANGED)

    @property
    def empty(self) -> bool:
        return not self.effective

    def as_json(self) -> dict[str, Any]:
        """The canonical form. `plan_hash` is taken over exactly this."""
        return {
            "organization": self.organization,
            "changes": [c.as_json() for c in self.effective],
            "renames": [list(r) for r in self.renames],
        }

    def plan_hash(self) -> str:
        return canonical_hash(self.as_json())

    def is_stale(self, *, now: dt.datetime | None = None) -> bool:
        if self.created_at is None:
            return False
        moment = now or dt.datetime.now(dt.UTC)
        return (moment - self.created_at).total_seconds() > STALE_AFTER_SECONDS

    def render(self) -> str:
        body = "no changes" if self.empty else "\n".join(c.render() for c in self.effective)
        counts: dict[str, int] = {}
        for change in self.effective:
            counts[change.action] = counts.get(change.action, 0) + 1
        summary = ", ".join(f"{n} to {action}" for action, n in sorted(counts.items())) or "nothing"
        lines = [body, "", f"plan: {summary}", f"plan_hash: {self.plan_hash()}"]
        if self.warnings:
            lines.extend(["", "warnings (apply does not act on these):"])
            lines.extend(f"  · {w}" for w in self.warnings)
        return "\n".join(lines)


# --- building the plan -------------------------------------------------------------------


def build_plan(
    org: CompiledOrg,
    state: OrgState,
    *,
    organization_id: UUID,
    renames: dict[str, str] | None = None,
    allow_replace: bool = False,
    now: dt.datetime | None = None,
) -> Plan:
    """Diff a compiled document set against live state.

    `renames` maps an old actor name to a new one and is how §4b's irreversible action
    is made deliberate. `allow_replace` is the other half of that answer: it says "the
    actor I removed and the actor I added are genuinely different actors", which is the
    only other thing the removal-plus-creation shape can mean.
    """
    applied_renames = dict(renames or {})
    changes: list[Change] = []
    warnings: list[str] = []

    if not state.exists:
        changes.append(Change("Organization", org.organization, CREATE, {"name": org.organization}))
    elif state.name != org.organization:
        changes.append(
            Change(
                "Organization",
                org.organization,
                UPDATE,
                {"name": [state.name, org.organization]},
            )
        )
    else:
        changes.append(Change("Organization", org.organization, UNCHANGED))

    changes.extend(_diff_roles(org, state))
    changes.extend(_diff_connections(org, state))
    changes.extend(_diff_actors(org, state, applied_renames))
    changes.extend(_diff_policies(org, state))
    changes.extend(_diff_grants(org, state))
    changes.extend(_diff_triggers(org, state))
    changes.extend(_diff_budget(org, state, organization_id))
    changes.extend(_diff_documents(org, state))

    warnings.extend(_orphan_warnings(org, state))
    _check_rename_intent(changes, state, applied_renames, allow_replace=allow_replace)
    _check_live_runs(changes, state)

    ordered = tuple(sorted(changes, key=lambda c: (c.kind, c.name)))
    return Plan(
        organization=org.organization,
        organization_id=organization_id,
        changes=ordered,
        warnings=tuple(sorted(set(warnings))),
        renames=tuple(sorted(applied_renames.items())),
        created_at=now or dt.datetime.now(dt.UTC),
    )


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        key: [before.get(key), after.get(key)]
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def _diff_roles(org: CompiledOrg, state: OrgState) -> list[Change]:
    live = {r.name: r for r in state.roles}
    out: list[Change] = []
    for role in org.roles:
        wanted = {
            "rank": role.rank,
            "parent": role.parent,
            "department": role.department,
            "approver": role.approver,
            "approverDailyBudget": role.approver_daily_budget,
            "baseAuthority": role.base_authority,
        }
        current = live.get(role.name)
        if current is None:
            out.append(Change("Role", role.name, CREATE, wanted))
            continue
        delta = _delta(
            {
                "rank": current.rank,
                "parent": current.parent,
                "department": current.department,
                "approver": current.approver,
                "approverDailyBudget": current.approver_daily_budget,
                "baseAuthority": current.base_authority,
            },
            wanted,
        )
        out.append(Change("Role", role.name, UPDATE if delta else UNCHANGED, delta))
    return out


def _diff_connections(org: CompiledOrg, state: OrgState) -> list[Change]:
    live = {c.name: c for c in state.connections}
    out: list[Change] = []
    for connection in org.connections:
        wanted = {
            "provider": connection.provider,
            "scopes": list(connection.scopes),
            "credential": connection.credential_name,
            "status": connection.status,
        }
        current = live.get(connection.name)
        if current is None:
            out.append(Change("Connection", connection.name, CREATE, wanted))
            continue
        delta = _delta(
            {
                "provider": current.provider,
                "scopes": list(current.scopes),
                "credential": current.credential_name,
                "status": current.status,
            },
            wanted,
        )
        out.append(Change("Connection", connection.name, UPDATE if delta else UNCHANGED, delta))
    return out


def _diff_actors(org: CompiledOrg, state: OrgState, renames: dict[str, str]) -> list[Change]:
    live = {a.name: a for a in state.actors}
    out: list[Change] = []

    for old, new in sorted(renames.items()):
        current = live.get(old)
        if current is None:
            raise RenameRefused(
                f"--rename {old}={new}: there is no actor named {old!r} in this organization"
            )
        if new in live:
            raise RenameRefused(
                f"--rename {old}={new}: an actor named {new!r} already exists. A rename "
                "onto an existing name would merge two histories, which is not "
                "something this tool can undo."
            )
        if org.actor(new) is None:
            raise RenameRefused(f"--rename {old}={new}: no Actor document is named {new!r}")
        out.append(
            Change(
                "Actor",
                new,
                RENAME,
                {"from": old, "runs": current.run_count, "versions": current.version},
            )
        )

    renamed_from = {new: old for old, new in renames.items()}
    for actor in org.actors:
        source = renamed_from.get(actor.name)
        current = live.get(source) if source else live.get(actor.name)
        wanted = {
            "specHash": actor.spec_hash,
            "kind": actor.spec.kind.value,
            "role": actor.role,
            "department": actor.department,
            "reportsTo": actor.reports_to,
            "active": True,
        }
        if current is None:
            out.append(Change("Actor", actor.name, CREATE, wanted))
            continue
        if source is not None:
            # The rename change already stands for this actor; the field diff would
            # duplicate it under a name the database does not have yet.
            continue
        delta = _delta(
            {
                "specHash": current.spec_hash,
                "kind": current.kind,
                "role": current.role_name,
                "department": current.department,
                "reportsTo": current.reports_to,
                "active": current.active,
            },
            wanted,
        )
        out.append(Change("Actor", actor.name, UPDATE if delta else UNCHANGED, delta))

    wanted_names = org.actor_names | set(renames)
    for name, current in sorted(live.items()):
        if name in wanted_names or not current.active:
            continue
        out.append(
            Change(
                "Actor",
                name,
                DEACTIVATE,
                {
                    "runs": current.run_count,
                    "liveRuns": current.live_runs,
                    # Said explicitly in the plan, because "removed from the YAML" reads
                    # like a delete and this is not one (edge case 78).
                    "note": "deactivated, not deleted; versions and history are kept",
                },
            )
        )
    return out


def _diff_policies(org: CompiledOrg, state: OrgState) -> list[Change]:
    live = {(p["scope_type"], p["scope_id"], p["action"]): p for p in state.policies}
    out: list[Change] = []
    for policy in org.policies:
        key = (policy.scope_type, policy.scope_id, policy.action)
        name = f"{policy.scope_type}:{policy.scope_id}/{policy.action}"
        wanted = {
            "level": policy.level.value,
            "approverRole": policy.approver_role,
            "maxEscalations": policy.max_escalations,
            "onExpiry": policy.on_expiry.value,
            "ttlSeconds": policy.ttl_seconds,
        }
        current = live.get(key)
        if current is None:
            out.append(Change("AuthorityPolicy", name, CREATE, wanted))
            continue
        delta = _delta(
            {
                "level": current["level"],
                "approverRole": current["approver_role"],
                "maxEscalations": int(current["max_escalations"]),
                "onExpiry": current["on_expiry"],
                "ttlSeconds": int(current["ttl_seconds"]),
            },
            wanted,
        )
        out.append(Change("AuthorityPolicy", name, UPDATE if delta else UNCHANGED, delta))
    return out


def _diff_grants(org: CompiledOrg, state: OrgState) -> list[Change]:
    live = {(g.subject_type, g.subject_id, g.tool): g for g in state.grants}
    out: list[Change] = []
    wanted_keys: set[tuple[str, str, str]] = set()
    for grant in org.grants:
        key = (grant.subject_type, grant.subject_id, grant.tool)
        wanted_keys.add(key)
        name = f"{grant.subject_type}:{grant.subject_id}/{grant.tool}"
        wanted = {"connection": grant.connection, "revoked": False}
        current = live.get(key)
        if current is None:
            out.append(Change("ToolGrant", name, CREATE, wanted))
            continue
        delta = _delta({"connection": current.connection, "revoked": current.revoked}, wanted)
        out.append(Change("ToolGrant", name, UPDATE if delta else UNCHANGED, delta))

    for key, current in sorted(live.items()):
        if key in wanted_keys or current.revoked:
            continue
        name = f"{current.subject_type}:{current.subject_id}/{current.tool}"
        out.append(
            Change(
                "ToolGrant",
                name,
                DEACTIVATE,
                {"note": "revoked, not deleted; the row stays for the denial-stream review"},
            )
        )
    return out


def _diff_triggers(org: CompiledOrg, state: OrgState) -> list[Change]:
    live = {t.key: t for t in state.triggers}
    out: list[Change] = []
    for trigger in org.triggers:
        wanted = {
            "actor": trigger.actor,
            "cron": trigger.cron,
            "timezone": trigger.timezone,
            "input": trigger.input,
            "catchup": trigger.catchup.value,
            "active": True,
        }
        current = live.get(trigger.key)
        if current is None:
            out.append(Change("Trigger", trigger.key, CREATE, wanted))
            continue
        delta = _delta(
            {
                "actor": current.actor_name,
                "cron": current.cron,
                "timezone": current.timezone,
                "input": current.input,
                "catchup": current.catchup,
                "active": current.active,
            },
            wanted,
        )
        out.append(Change("Trigger", trigger.key, UPDATE if delta else UNCHANGED, delta))

    wanted_keys = {t.key for t in org.triggers}
    for key, current in sorted(live.items()):
        if key in wanted_keys or not current.active:
            continue
        out.append(Change("Trigger", key, DEACTIVATE, {"note": "deactivated, not deleted"}))
    return out


def _diff_budget(org: CompiledOrg, state: OrgState, organization_id: UUID) -> list[Change]:
    """Pool limits for the current period, and the refusal edge case 79 names.

    `budget_pools.scope_id` is a uuid — an org id, a derived department id, an actor id
    — so every level's live limit is looked up through the same derivation the budget
    service uses. Doing it here rather than at apply time is what lets the *plan* say
    "this lowers marketing from 60000c to 20000c, and 31400c is already committed"
    before anybody types `apply`.
    """
    from runtime.budget.service import department_scope_id

    budget = org.budget
    if budget is None:
        return []

    actor_ids = {a.name: a.id for a in state.actors}
    wanted: list[tuple[str, str, int]] = [
        (f"org:{organization_id}", "organization", budget.organization),
    ]
    wanted.extend(
        (
            f"department:{department_scope_id(OrganizationId(organization_id), name)}",
            f"department:{name}",
            cents,
        )
        for name, cents in sorted(budget.departments.items())
    )
    wanted.extend(
        (f"actor:{actor_ids[name]}", f"actor:{name}", cents)
        for name, cents in sorted(budget.actors.items())
        if name in actor_ids
    )

    out: list[Change] = []
    for pool_key, label, cents in wanted:
        live = state.pools.get(pool_key)
        if live is None:
            out.append(Change("BudgetPolicy", label, CREATE, {"limitCents": cents}))
            continue
        limit, held = live
        if limit == cents:
            out.append(Change("BudgetPolicy", label, UNCHANGED))
            continue
        if cents < held:
            raise SpecValidationError(
                f"BudgetPolicy would set {label} to {cents}c, but {held}c is already "
                "committed or reserved this period. A pool whose limit is below what it "
                "already holds refuses every reservation, including ones already made."
            )
        out.append(Change("BudgetPolicy", label, UPDATE, {"limitCents": [limit, cents]}))
    # An actor the budget names but the database has not created yet is not an error:
    # the same apply is about to create it, and the pool is created at its first run.
    for name in sorted(set(budget.actors) - set(actor_ids)):
        out.append(
            Change("BudgetPolicy", f"actor:{name}", CREATE, {"limitCents": budget.actors[name]})
        )
    return out


def _diff_documents(org: CompiledOrg, state: OrgState) -> list[Change]:
    """`spec_documents` rows, so `spec drift` has something to compare against.

    Kept out of the visible plan when unchanged, like everything else — but a document
    whose hash moved is reported even when nothing it projects onto moved, because a
    `Department` document has no other footprint in the schema and its edits would
    otherwise be invisible.
    """
    out: list[Change] = []
    for doc in org.documents:
        current = state.document(doc.kind.value, doc.name)
        if current is None:
            out.append(
                Change(
                    "Document",
                    f"{doc.kind.value}/{doc.name}",
                    CREATE,
                    {"hash": doc.spec_hash},
                )
            )
        elif current.spec_hash != doc.spec_hash:
            out.append(
                Change(
                    "Document",
                    f"{doc.kind.value}/{doc.name}",
                    UPDATE,
                    {"hash": [current.spec_hash, doc.spec_hash]},
                )
            )
        else:
            out.append(Change("Document", f"{doc.kind.value}/{doc.name}", UNCHANGED))
    return out


def _orphan_warnings(org: CompiledOrg, state: OrgState) -> list[str]:
    """Rows the documents do not mention and that `apply` will not touch (edge case 87).

    Reported rather than removed. `apply` is not a reconciler and does not auto-heal;
    `spec drift` is where this is somebody's problem, and it is deliberately a report.
    """
    out: list[str] = []
    role_names = {r.name for r in org.roles}
    for role in state.roles:
        if role.name not in role_names:
            out.append(
                f"role {role.name!r} exists in the database and in no document; apply "
                "leaves it alone"
            )
    connection_names = {c.name for c in org.connections}
    for connection in state.connections:
        if connection.name not in connection_names:
            out.append(
                f"connection {connection.name!r} exists in the database and in no "
                "document; apply leaves it alone"
            )
    policy_keys = {(p.scope_type, p.scope_id, p.action) for p in org.policies}
    for policy in state.policies:
        key = (policy["scope_type"], policy["scope_id"], policy["action"])
        if key not in policy_keys:
            out.append(
                f"authority policy {key[0]}:{key[1]}/{key[2]} exists in the database and "
                "in no document; apply leaves it alone"
            )
    return out


def _check_rename_intent(
    changes: list[Change],
    state: OrgState,
    renames: dict[str, str],
    *,
    allow_replace: bool,
) -> None:
    """§4b / edge case 77 — make an irreversible action deliberate.

    A rename is indistinguishable from a delete plus a create: the documents contain a
    name the database does not, and the database contains a name the documents do not.
    Nothing in the files can tell the two apart, so the tool refuses to guess when the
    stakes are high — when the disappearing actor has run history to lose.

    `--rename old=new` says it is a rename. `--allow-replace` says it is not. Either
    way somebody has said it, which is the whole cost of option (b) and the whole
    benefit.
    """
    if allow_replace or renames:
        return
    losing = [
        c
        for c in changes
        if c.kind == "Actor" and c.action == DEACTIVATE and int(c.detail.get("runs", 0)) > 0
    ]
    creating = [c for c in changes if c.kind == "Actor" and c.action == CREATE]
    if not losing or not creating:
        return
    old = ", ".join(sorted(c.name for c in losing))
    new = ", ".join(sorted(c.name for c in creating))
    del state
    raise RenameRefused(
        f"this plan removes actor(s) with run history ({old}) and creates new one(s) "
        f"({new}). If that is a rename, say so: --rename OLD=NEW. Renaming is a delete "
        "plus a create — the new actor has no version history, no memory and no task "
        "history — so it is not something to infer from a diff. If they really are "
        "different actors, pass --allow-replace."
    )


def _check_live_runs(changes: list[Change], state: OrgState) -> None:
    """Edge case 73: deactivating an actor with runs in flight orphans them.

    Refused at plan time. The `--drain` alternative the plan names is deliberately not
    implemented as a flag that waits: a CLI that blocks for an unknown number of minutes
    holding an advisory lock is worse than one that tells you what to do. Engage the
    kill switch in `drain` mode, let the runs finish, then apply.
    """
    del state
    blocked = [
        c
        for c in changes
        if c.kind == "Actor" and c.action == DEACTIVATE and int(c.detail.get("liveRuns", 0)) > 0
    ]
    if not blocked:
        return
    detail = ", ".join(
        f"{c.name} ({c.detail['liveRuns']} live)" for c in sorted(blocked, key=lambda c: c.name)
    )
    raise SpecValidationError(
        f"cannot deactivate {detail}: those runs are QUEUED or RUNNING and would be "
        "orphaned. Drain first — `runtime.cli killswitch --engage --scope actor "
        "--scope-id <name> --mode drain` — and apply once they have finished."
    )


__all__ = [
    "CREATE",
    "DEACTIVATE",
    "RENAME",
    "STALE_AFTER_SECONDS",
    "UNCHANGED",
    "UPDATE",
    "Change",
    "Plan",
    "build_plan",
]
