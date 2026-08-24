"""Drift — what the database has that the files do not say, and vice versa.

Edge case 87: *the database changed outside YAML*. Somebody ran `runtime.cli
credentials`, or granted a tool by hand during an incident, or a `seed_department` ran
on a boot. All of those are legitimate; what is not legitimate is nobody knowing.

**Drift reports. It never heals.** `apply` is not a reconciler (§2) and this is the
module where that decision is felt: the obvious next feature is `--fix`, and the
obvious next feature after that is a daemon, and then the org rewrites itself during a
measurement window. What this gives you instead is a list and an exit code, which is
enough for a cron to page somebody and not enough for anything to change on its own.

Two kinds of finding, and they mean different things:

- **`config`** — a live row disagrees with what the documents compile to. Somebody
  edited the database, or the documents were never applied.
- **`document`** — the stored `spec_documents` row for a name has a different hash from
  the file on disk. The files moved; nobody applied them. This is the honest answer to
  "is my checkout what is running", and it is the one `plan` would also tell you — but
  `drift` answers it without computing a plan, so it works when a plan would refuse
  (a live run, a pending rename).
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.persistence.repositories.spec import OrgState
from runtime.spec.compile import CompiledOrg


@dataclass(frozen=True, slots=True)
class Drift:
    source: str
    """`config` or `document` — see the module docstring."""
    subject: str
    detail: str

    def render(self) -> str:
        return f"[{self.source}] {self.subject}: {self.detail}"


@dataclass(frozen=True, slots=True)
class DriftReport:
    findings: tuple[Drift, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings

    def render(self) -> str:
        if self.clean:
            return "no drift: the database matches the documents"
        lines = [f"{len(self.findings)} finding(s):", ""]
        lines.extend(f"  {f.render()}" for f in self.findings)
        lines.extend(
            [
                "",
                "`spec drift` reports and does not heal. Run `spec plan` to see what an "
                "apply would do about it, or reconcile the files to the database by hand.",
            ]
        )
        return "\n".join(lines)


def detect_drift(org: CompiledOrg, state: OrgState) -> DriftReport:
    """Compare compiled documents against live state and stored documents.

    One section per kind, each with its own `live_*` index that is *consumed* as it
    goes: what remains at the end of a section is exactly what the database has and the
    files do not, which is the direction of drift nobody thinks to look for.
    """
    findings: list[Drift] = []

    if not state.exists:
        return DriftReport(
            (Drift("config", org.organization, "the organization does not exist in the database"),)
        )

    # --- documents -------------------------------------------------------------------
    live_documents = {(d.kind, d.name): d for d in state.documents}
    for document in org.documents:
        label = f"{document.kind.value}/{document.name}"
        stored = live_documents.pop((document.kind.value, document.name), None)
        if stored is None:
            findings.append(Drift("document", label, "in the files, never applied"))
        elif stored.spec_hash != document.spec_hash:
            findings.append(
                Drift(
                    "document",
                    label,
                    f"file hash {document.spec_hash} vs applied {stored.spec_hash} "
                    f"(applied {stored.applied_at.isoformat()} by {stored.applied_by})",
                )
            )
    for (kind, name), stored in sorted(live_documents.items()):
        findings.append(
            Drift(
                "document",
                f"{kind}/{name}",
                f"applied {stored.applied_at.isoformat()} and absent from the files",
            )
        )

    # --- actors ----------------------------------------------------------------------
    live_actors = {a.name: a for a in state.actors}
    for actor in org.actors:
        label = f"Actor/{actor.name}"
        row = live_actors.pop(actor.name, None)
        if row is None:
            findings.append(Drift("config", label, "in the files, not in the database"))
            continue
        if not row.active:
            findings.append(Drift("config", label, "deactivated in the database"))
        if row.spec_hash != actor.spec_hash:
            findings.append(
                Drift(
                    "config",
                    label,
                    f"active version {row.version} has spec_hash {row.spec_hash}; "
                    f"the files compile to {actor.spec_hash}",
                )
            )
        for field_name, live, wanted in (
            ("role", row.role_name, actor.role),
            ("department", row.department, actor.department),
            ("reportsTo", row.reports_to, actor.reports_to),
        ):
            if live != wanted:
                findings.append(
                    Drift("config", label, f"{field_name} is {live!r}, files say {wanted!r}")
                )
    for name, row in sorted(live_actors.items()):
        if row.active:
            findings.append(
                Drift("config", f"Actor/{name}", "active in the database and absent from the files")
            )

    # --- roles -------------------------------------------------------------------------
    live_roles = {r.name: r for r in state.roles}
    for role in org.roles:
        label = f"Role/{role.name}"
        role_row = live_roles.pop(role.name, None)
        if role_row is None:
            findings.append(Drift("config", label, "in the files, not in the database"))
            continue
        if (role_row.rank, role_row.parent, role_row.approver) != (
            role.rank,
            role.parent,
            role.approver,
        ):
            findings.append(
                Drift(
                    "config",
                    label,
                    f"database has rank={role_row.rank} parent={role_row.parent!r} "
                    f"approver={role_row.approver!r}; files say rank={role.rank} "
                    f"parent={role.parent!r} approver={role.approver!r}",
                )
            )
    for name in sorted(live_roles):
        findings.append(
            Drift("config", f"Role/{name}", "in the database and absent from the files")
        )

    # --- connections --------------------------------------------------------------------
    live_connections = {c.name: c for c in state.connections}
    for connection in org.connections:
        label = f"Connection/{connection.name}"
        connection_row = live_connections.pop(connection.name, None)
        if connection_row is None:
            findings.append(Drift("config", label, "in the files, not in the database"))
        elif (
            connection_row.provider,
            connection_row.credential_name,
            connection_row.status,
        ) != (connection.provider, connection.credential_name, connection.status):
            findings.append(
                Drift(
                    "config",
                    label,
                    f"database has provider={connection_row.provider!r} "
                    f"credential={connection_row.credential_name!r} "
                    f"status={connection_row.status!r}",
                )
            )
    for name in sorted(live_connections):
        findings.append(
            Drift("config", f"Connection/{name}", "in the database and absent from the files")
        )

    # --- grants ---------------------------------------------------------------------------
    live_grants = {(g.subject_type, g.subject_id, g.tool): g for g in state.grants if not g.revoked}
    for grant in org.grants:
        grant_key = (grant.subject_type, grant.subject_id, grant.tool)
        label = f"ToolGrant/{grant.subject_type}:{grant.subject_id}/{grant.tool}"
        grant_row = live_grants.pop(grant_key, None)
        if grant_row is None:
            findings.append(Drift("config", label, "in the files, not granted in the database"))
        elif grant_row.connection != grant.connection:
            findings.append(
                Drift(
                    "config",
                    label,
                    f"connection is {grant_row.connection!r}, files say {grant.connection!r}",
                )
            )
    for subject_type, subject_id, tool in sorted(live_grants):
        findings.append(
            Drift(
                "config",
                f"ToolGrant/{subject_type}:{subject_id}/{tool}",
                "granted in the database and absent from the files — a hand-granted tool "
                "is exactly what this report is for",
            )
        )

    # --- authority policies -----------------------------------------------------------------
    live_policies = {(p["scope_type"], p["scope_id"], p["action"]): p for p in state.policies}
    for policy in org.policies:
        policy_key = (policy.scope_type, policy.scope_id, policy.action)
        label = f"AuthorityPolicy/{policy.scope_type}:{policy.scope_id}/{policy.action}"
        policy_row = live_policies.pop(policy_key, None)
        if policy_row is None:
            findings.append(Drift("config", label, "in the files, not in the database"))
        elif (
            policy_row["level"] != policy.level.value
            or policy_row["approver_role"] != policy.approver_role
        ):
            findings.append(
                Drift(
                    "config",
                    label,
                    f"database has level={policy_row['level']!r} "
                    f"approver={policy_row['approver_role']!r}; files say "
                    f"level={policy.level.value!r} approver={policy.approver_role!r}",
                )
            )
    for scope_type, scope_id, action in sorted(live_policies):
        findings.append(
            Drift(
                "config",
                f"AuthorityPolicy/{scope_type}:{scope_id}/{action}",
                "in the database and absent from the files",
            )
        )

    # --- triggers ------------------------------------------------------------------------
    live_triggers = {t.key: t for t in state.triggers if t.active}
    for trigger in org.triggers:
        label = f"Trigger/{trigger.key}"
        trigger_row = live_triggers.pop(trigger.key, None)
        if trigger_row is None:
            findings.append(Drift("config", label, "in the files, not active in the database"))
        elif (trigger_row.actor_name, trigger_row.cron) != (trigger.actor, trigger.cron):
            findings.append(
                Drift(
                    "config",
                    label,
                    f"database has actor={trigger_row.actor_name!r} cron={trigger_row.cron!r}; "
                    f"files say actor={trigger.actor!r} cron={trigger.cron!r}",
                )
            )
    for key in sorted(live_triggers):
        findings.append(
            Drift("config", f"Trigger/{key}", "active in the database and absent from the files")
        )

    return DriftReport(tuple(findings))


__all__ = ["Drift", "DriftReport", "detect_drift"]
