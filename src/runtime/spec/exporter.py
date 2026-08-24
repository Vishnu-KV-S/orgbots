"""The Python-defined department, as documents.

**This ships before the importer** (M4 §10), and the ordering is the point. Exporting
what already exists gives a known-correct corpus to develop the loader and the compiler
against, instead of hand-writing YAML and debugging two things at once. It is also half
of §8's round-trip test: export, load, compile, compare hashes.

**Nothing here is a translation with judgement in it.** Every value comes from
`runtime.org.department` or `runtime.org.governance_seed`; where the Python has no
equivalent — `reportsTo`, a `Department` document — the exporter emits the fact the
code already implies rather than inventing a new one. `reportsTo` is the clearest case:
`department.ASSIGNEES` is exactly "who the head may assign to", so the assignees report
to the head, and writing that down changes no `ActorSpec` and therefore no `spec_hash`.

**Model profiles get derived names.** Three distinct profiles exist in M1 — pro, pro
with provider-side search and thinking off, flash — and they are named after the model
with a suffix for whatever makes them differ. The names are derived rather than chosen
so that a profile added in code appears in the export without anybody having to name it,
and a rename in code shows up as a rename in the diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from runtime.budget.service import (
    DEFAULT_DEPARTMENT_LIMIT_CENTS,
    DEFAULT_ORG_LIMIT_CENTS,
    OVERSUBSCRIPTION_K,
)
from runtime.domain.hashing import canonical_json
from runtime.domain.specs import ActorSpec, ModelProfile
from runtime.org import governance_seed as gov
from runtime.org.department import (
    ASSIGNEES,
    DEPARTMENT_NAME,
    DEPARTMENT_SPECS,
    HEAD,
    TRIGGERS,
)
from runtime.spec.documents import (
    API_VERSION,
    ActorDoc,
    AuthorityPolicyDoc,
    BudgetPolicyDoc,
    CeilingsDoc,
    ConnectionDoc,
    DepartmentDoc,
    Document,
    DocumentSet,
    GrantSubject,
    Kind,
    Metadata,
    ModelProfileDoc,
    OrganizationDoc,
    RoleDoc,
    SecretRef,
    ToolGrantDoc,
    TriggerDoc,
)
from runtime.spec.loader import dump_documents

DEFAULT_ORGANIZATION = "acme"
"""`seed_department`'s default organization name. Overridable on export."""


@dataclass(frozen=True, slots=True)
class Export:
    documents: DocumentSet
    profile_names: dict[str, str]
    """canonical JSON of a `ModelProfile` → the document name it was given."""

    def to_yaml(self) -> str:
        return dump_documents(self.documents.documents)


def export_department(
    *,
    organization: str = DEFAULT_ORGANIZATION,
    specs: tuple[ActorSpec, ...] = DEPARTMENT_SPECS,
    with_budget: bool = True,
) -> Export:
    """Everything `seed_department` + `seed_governance` install, as documents."""
    profiles, profile_names = _model_profiles(specs)

    documents: list[Document] = [
        _doc(Kind.ORGANIZATION, organization, OrganizationDoc(display_name=organization)),
        _doc(
            Kind.DEPARTMENT,
            DEPARTMENT_NAME,
            DepartmentDoc(
                head=HEAD,
                description="The M1 marketing department: plan, research, draft, gate, measure.",
            ),
        ),
    ]
    documents.extend(
        _doc(Kind.MODEL_PROFILE, name, body) for name, body in sorted(profiles.items())
    )
    documents.extend(
        _doc(
            Kind.ROLE,
            role.name,
            RoleDoc(
                rank=role.rank,
                parent=role.parent,
                department=role.department,
                approver=role.approver,
                approver_daily_budget=role.approver_daily_budget,
                base_authority=dict(role.base_authority),
            ),
        )
        for role in gov.ROLES
    )

    placement = {p.actor: p for p in gov.PLACEMENTS}
    for spec in specs:
        seat = placement.get(spec.name)
        documents.append(
            _doc(
                Kind.ACTOR,
                spec.name,
                ActorDoc(
                    kind=spec.kind,
                    department=seat.department if seat else None,
                    role=seat.role if seat else None,
                    # Not in the Python config, and implied by it: `ASSIGNEES` is
                    # exactly "who the head may assign to". Recording the edge changes
                    # no ActorSpec field and therefore no spec_hash.
                    reports_to=HEAD if spec.name in ASSIGNEES else None,
                    graph=spec.graph_ref,
                    handler=spec.handler_ref,
                    tools=tuple(sorted(spec.allowed_tools)),
                    model_profiles={
                        work_class.value: profile_names[canonical_json(profile)]
                        for work_class, profile in sorted(
                            spec.model_profiles.profiles.items(), key=lambda kv: kv[0].value
                        )
                    },
                    ceilings=CeilingsDoc(
                        max_llm_calls=spec.ceilings.max_llm_calls,
                        max_tool_calls=spec.ceilings.max_tool_calls,
                        max_wall_clock_s=spec.ceilings.max_wall_clock_s,
                        max_cost_cents=spec.ceilings.max_cost_cents,
                        max_depth=spec.ceilings.max_depth,
                    ),
                ),
            )
        )

    documents.extend(
        _doc(
            Kind.CONNECTION,
            connection.name,
            ConnectionDoc(
                provider=connection.provider,
                scopes=tuple(connection.scopes),
                # Never the value. `credential_name` is a *pointer* into the encrypted
                # store migration 022 created, which is why the format can have a field
                # here at all without §3's inline-secret rule being a fiction.
                credentials=(
                    SecretRef(secret_ref=connection.credential_name)
                    if connection.credential_name
                    else None
                ),
            ),
        )
        for connection in gov.CONNECTIONS
    )

    documents.extend(
        _doc(
            Kind.TOOL_GRANT,
            _grant_name(grant.subject_type, grant.subject_id, grant.tool),
            ToolGrantDoc(
                subject=GrantSubject(
                    actor=grant.subject_id if grant.subject_type == "actor" else None,
                    role=grant.subject_id if grant.subject_type == "role" else None,
                ),
                tool=grant.tool,
                connection=grant.connection,
            ),
        )
        for grant in gov.GRANTS
    )

    documents.extend(
        _doc(
            Kind.AUTHORITY_POLICY,
            f"{policy.scope_type}-{policy.scope_id}-{policy.action}".replace("_", "-"),
            AuthorityPolicyDoc(
                # `scope_type` is the same closed set on both sides — 'role',
                # 'department', 'actor' — but one is a `str` field on a dataclass and the
                # other a `Literal`, so the narrowing has to be written down.
                scope=cast(Literal["role", "department", "actor"], policy.scope_type),
                target=policy.scope_id,
                action=policy.action,
                level=policy.level,
                approver_role=policy.approver_role,
                max_escalations=policy.max_escalations,
                on_expiry=policy.on_expiry,
                ttl_seconds=policy.ttl_seconds,
            ),
        )
        for policy in gov.POLICIES
    )

    documents.extend(
        _doc(
            Kind.TRIGGER,
            trigger.key,
            TriggerDoc(
                actor=trigger.actor_name,
                cron=trigger.cron,
                mode=trigger.mode,
                catchup=trigger.catchup,
            ),
        )
        for trigger in TRIGGERS
    )

    if with_budget:
        # Org and department only, matching what `BudgetService`'s defaults already
        # produce. Per-actor limits are deliberately **not** exported: the runtime gives
        # every actor `DEFAULT_ACTOR_LIMIT_CENTS` independently, which sums well past
        # the department pool — legal, because reservations are bounded per level and
        # allocations are advisory, but not something to write down as an *allocation*.
        # Applying this document therefore changes no pool limit, which is the property
        # M4 needs.
        documents.append(
            _doc(
                Kind.BUDGET_POLICY,
                "default",
                BudgetPolicyDoc(
                    oversubscription=OVERSUBSCRIPTION_K,
                    organization=DEFAULT_ORG_LIMIT_CENTS,
                    departments={DEPARTMENT_NAME: DEFAULT_DEPARTMENT_LIMIT_CENTS},
                ),
            )
        )

    return Export(documents=DocumentSet.build(documents), profile_names=profile_names)


# --- helpers ----------------------------------------------------------------------------


def _doc(kind: Kind, name: str, spec: object) -> Document:
    from runtime.spec.documents import Spec

    assert isinstance(spec, Spec)
    body = {
        "apiVersion": API_VERSION,
        "kind": kind.value,
        "metadata": {"name": name},
        "spec": spec.model_dump(mode="json", by_alias=True),
    }
    import yaml

    return Document(
        kind=kind,
        metadata=Metadata(name=name),
        spec=spec,
        source="<exported>",
        source_yaml=yaml.safe_dump(body, sort_keys=True, default_flow_style=False),
        index=0,
    )


def _grant_name(subject_type: str, subject_id: str, tool: str) -> str:
    """`actor-research-web-search-1`. Derived, so re-exporting is stable."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{subject_type}-{subject_id}-{tool}".lower()).strip("-")
    return slug


def _profile_slug(profile: ModelProfile) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", profile.model.lower()).strip("-")
    parts = [base]
    if profile.web_search:
        parts.append("search")
    if not profile.thinking:
        parts.append("nothink")
    if profile.effort:
        parts.append(profile.effort)
    return "-".join(parts)


def _model_profiles(
    specs: tuple[ActorSpec, ...],
) -> tuple[dict[str, ModelProfileDoc], dict[str, str]]:
    """Every distinct profile in the department, named and deduplicated.

    Keyed on the profile's canonical JSON, so two actors sharing a profile share one
    document — which is what makes "swap SUMMARIZATION onto a cheaper model" a one-line
    diff rather than a change in four actor documents.
    """
    seen: dict[str, ModelProfile] = {}
    for spec in specs:
        for profile in spec.model_profiles.profiles.values():
            seen.setdefault(canonical_json(profile), profile)

    names: dict[str, str] = {}
    used: dict[str, int] = {}
    documents: dict[str, ModelProfileDoc] = {}
    for key in sorted(seen):
        profile = seen[key]
        slug = _profile_slug(profile)
        count = used.get(slug, 0) + 1
        used[slug] = count
        # A numeric suffix only on a genuine collision — two profiles that differ in a
        # field the slug does not encode, e.g. price alone. Deterministic because the
        # keys are walked in sorted canonical order.
        name = slug if count == 1 else f"{slug}-{count}"
        names[key] = name
        documents[name] = ModelProfileDoc(
            provider=profile.provider,
            model=profile.model,
            max_output_tokens=profile.max_output_tokens,
            temperature=profile.temperature,
            input_cents_per_mtok=profile.input_cents_per_mtok,
            output_cents_per_mtok=profile.output_cents_per_mtok,
            thinking=profile.thinking,
            effort=profile.effort,
            web_search=profile.web_search,
        )
    return documents, names


__all__ = ["DEFAULT_ORGANIZATION", "Export", "export_department"]
