"""The department's governance configuration, as values.

Same principle as `runtime.org.department`: no YAML, no control plane, everything a
value in a module. §12's hardcoding is what makes the measurement fast enough to be
honest, and governance is not an exception — a policy in a config file is a policy
somebody widens without a diff.

**The role tree.** Four roles, and the shape is the escalation order:

    operator   rank 0   — the buck stops here. Answers as "operator".
      director rank 5   — first line for the marketing department. Answers as
                          "director".
        head   rank 10  — marketing-head. Requests approvals; grants none.
          ic   rank 20  — research, content, analytics. Requests nothing.

`director` sits between `head` and `operator` for one reason: it gives the escalation
chain somewhere to go. With a two-level tree, `max_escalations` could only ever be
zero and T33 would be testing an escalation that does not exist. It is also what the
acyclicity check has to have real work to do on — a chain of length one is trivially
acyclic and would prove nothing.

**The policy.** `publish_external` requires a human for the whole marketing
department, first answered by the director, escalating once to the operator, and
denying at the end of the chain. That is deliberately *not* the loosest thing that
would pass: a policy naming `operator` directly would never escalate, which is the
configuration most organizations end up with and the one that makes an approval queue
one person's problem.

**Grants.** The head holds `publish.external@1`; research holds search and fetch;
content holds fetch. That mirrors M1's `allowed_tools` on purpose — the two checks
answer different questions (admitted-to versus may-now) and disagreement between them
is a bug, so they are written from the same table below.

**Rate limits.** Three scopes, each with a number chosen against a real failure. The
per-actor limit is the one worth defending: it is not about cost, which the budget
already bounds, but about a graph looping. Sixty tool calls a minute is far above what
any M1 graph does and far below what a loop does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from runtime.domain.enums import AuthorityLevel, OnExpiry
from runtime.domain.ids import OrganizationId
from runtime.org.department import ANALYTICS, CONTENT, HEAD, RESEARCH

DEPARTMENT = "marketing"

OPERATOR_ROLE = "operator"
DIRECTOR_ROLE = "director"
HEAD_ROLE = "head"
IC_ROLE = "ic"

ACTION_PUBLISH = "publish_external"


@dataclass(frozen=True, slots=True)
class RoleSpec:
    name: str
    rank: int
    parent: str | None
    department: str | None = None
    approver: str | None = None
    approver_daily_budget: int = 10
    base_authority: dict[str, object] = field(default_factory=dict)


ROLES = (
    RoleSpec(
        name=OPERATOR_ROLE,
        rank=0,
        parent=None,
        approver="operator",
        # Deliberately small. §10 says to treat this alert firing as a design problem
        # rather than a threshold to raise, and a limit set high enough never to fire
        # is a limit that cannot tell you anything.
        approver_daily_budget=10,
    ),
    RoleSpec(
        name=DIRECTOR_ROLE,
        rank=5,
        parent=OPERATOR_ROLE,
        department=DEPARTMENT,
        approver="director",
        approver_daily_budget=6,
    ),
    RoleSpec(name=HEAD_ROLE, rank=10, parent=DIRECTOR_ROLE, department=DEPARTMENT),
    RoleSpec(name=IC_ROLE, rank=20, parent=HEAD_ROLE, department=DEPARTMENT),
)


@dataclass(frozen=True, slots=True)
class PolicySpec:
    scope_type: str
    scope_id: str
    action: str
    level: AuthorityLevel
    approver_role: str | None = None
    max_escalations: int = 0
    on_expiry: OnExpiry = OnExpiry.DENY
    ttl_seconds: int = 24 * 60 * 60


POLICIES = (
    PolicySpec(
        scope_type="department",
        scope_id=DEPARTMENT,
        action=ACTION_PUBLISH,
        level=AuthorityLevel.HUMAN,
        approver_role=DIRECTOR_ROLE,
        # One escalation: director, then operator, then the chain is out and
        # `on_expiry` applies. T33 walks exactly this.
        max_escalations=1,
        on_expiry=OnExpiry.DENY,
    ),
)


@dataclass(frozen=True, slots=True)
class ActorPlacement:
    actor: str
    role: str
    department: str | None = DEPARTMENT


PLACEMENTS = (
    ActorPlacement(HEAD, HEAD_ROLE),
    ActorPlacement(RESEARCH, IC_ROLE),
    ActorPlacement(CONTENT, IC_ROLE),
    ActorPlacement(ANALYTICS, IC_ROLE),
)


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    name: str
    provider: str
    credential_name: str | None
    scopes: tuple[str, ...] = ()


CONNECTIONS = (
    ConnectionSpec("search-primary", "serper", "search_api_key", ("search:read",)),
    ConnectionSpec("publish-staging", "publisher", "publish_api_key", ("publish:write",)),
)


@dataclass(frozen=True, slots=True)
class GrantSpec:
    subject_type: str
    subject_id: str
    tool: str
    connection: str | None = None


GRANTS = (
    GrantSpec("actor", HEAD, "publish.external@1", "publish-staging"),
    GrantSpec("actor", RESEARCH, "web.search@1", "search-primary"),
    GrantSpec("actor", RESEARCH, "web.fetch@1"),
    GrantSpec("actor", CONTENT, "web.fetch@1"),
)
"""Deliberately actor-scoped rather than role-scoped.

`research` and `content` share the `ic` role but not their tools, and a role grant
would give `analytics` — the deterministic control actor whose whole value is that it
touches nothing — a web fetch it must not have. Role grants are the right shape when
a role's members really are interchangeable; here they are not, and pretending
otherwise to save three rows would quietly widen the one actor the M1 numbers are read
off.
"""


@dataclass(frozen=True, slots=True)
class RateLimitSpec:
    scope_type: str
    scope_id: str
    limit_per_window: int
    window_seconds: int = 60
    fail_open: bool = True


RATE_LIMITS = (
    # Per connection: the provider's own limit. Fail *closed* — a Redis outage that
    # let us hammer a search API past its quota gets the key banned, and the blast
    # radius of that is every run in the organization.
    RateLimitSpec("connection", "search-primary", limit_per_window=30, fail_open=False),
    RateLimitSpec("connection", "publish-staging", limit_per_window=10, fail_open=False),
    # Per provider: the aggregate. Two connections inside their own limits can still
    # put a vendor-wide quota over.
    RateLimitSpec("provider", "serper", limit_per_window=40),
    # There is no `anthropic` row because there is no `anthropic` provider: it is not
    # registered in `build_providers`, so no profile can route a call to it. The row was
    # dropped with the provider rather than left behind — `RateLimiter.check` treats an
    # absent row as unlimited, and a limit guarding a route that does not exist reads as
    # coverage while providing none. Re-registering the provider means re-adding this.
    RateLimitSpec("provider", "deepseek", limit_per_window=120),
    # Per actor: the loop guard. Fails *open*, because the budget still bounds the
    # damage and stopping the department to protect a guard is the worse outcome.
    *(
        RateLimitSpec("actor", actor, limit_per_window=60)
        for actor in (HEAD, RESEARCH, CONTENT, ANALYTICS)
    ),
)


# --- derived ids --------------------------------------------------------------------
#
# Derived rather than random, for the reason every other id in this codebase is:
# seeding twice must produce one of each, and an upsert needs a stable key to conflict
# on.


def role_id_for(organization_id: OrganizationId, name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"role:{organization_id}:{name}")


def policy_id_for(
    organization_id: OrganizationId, scope_type: str, scope_id: str, action: str
) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"policy:{organization_id}:{scope_type}:{scope_id}:{action}"
    )


def connection_id_for(organization_id: OrganizationId, name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"connection:{organization_id}:{name}")


def grant_id_for(
    organization_id: OrganizationId, subject_type: str, subject_id: str, tool: str
) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"grant:{organization_id}:{subject_type}:{subject_id}:{tool}"
    )


def rate_limit_id_for(organization_id: OrganizationId, scope_type: str, scope_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ratelimit:{organization_id}:{scope_type}:{scope_id}")
