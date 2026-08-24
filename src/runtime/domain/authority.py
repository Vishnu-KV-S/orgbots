"""Resolved authority — the frozen answer to "what may this actor do".

M1's authority was a module-level dict read at the moment of the call. M2's is a
*value*, resolved once at `start_run()` from roles, department policy and actor
overrides, and hashed into `spec_hash` along with the rest of the compiled spec.

That change is the whole point of §3. A resolver read live at call time means a
policy edit lands mid-run, so two calls in the same run can be governed by different
rules and nothing records which. A frozen value means a run executes under exactly
one authority for its whole life, and `spec_hash` is the receipt.

The one thing deliberately *not* frozen is revocation. A grant withdrawn while a run
is in flight must take effect inside that run, and the mechanism is a ≤30s permission
re-check against `tool_grants` (edge case 64) — not a mutation of this object, which
is immutable, and not a re-resolution, which would reopen the hole above.

Everything here is pure: no I/O, no clock, no database. `runtime.org.authority`
builds these from rows; `runtime.gateway` reads them.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.enums import AuthorityLevel, BlastRadius, OnExpiry


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class ActionAuthority(Frozen):
    """What it takes to perform one action.

    `approver_chain` is ordered and it is the escalation order: index 0 answers
    first, index 1 answers if index 0 lets the TTL elapse, and so on. It is resolved
    from the role tree at compile time so that an approval escalates under the policy
    it was created under rather than whatever policy exists when the TTL fires.
    """

    action: str = Field(min_length=1)
    level: AuthorityLevel
    approver_chain: tuple[str, ...] = ()
    max_escalations: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    on_expiry: OnExpiry = OnExpiry.DENY
    source: str = "default"
    """Which resolution step produced this — `role:head`, `department:marketing`,
    `actor:marketing-head`, `blast_radius_floor`, `default`. Written into the audit
    row's `reason`, because "denied by policy" without naming the policy is the kind
    of audit line that costs an hour during an incident."""

    @model_validator(mode="after")
    def _check_chain(self) -> Self:
        if self.level is AuthorityLevel.HUMAN and not self.approver_chain:
            raise ValueError(
                f"action {self.action!r} requires a human but names no approver; "
                "an approval nobody can answer is a deadlock with extra steps"
            )
        if self.max_escalations >= len(self.approver_chain) and self.approver_chain:
            # The chain must be able to absorb every escalation the policy permits.
            raise ValueError(
                f"action {self.action!r} allows {self.max_escalations} escalations "
                f"but the chain has {len(self.approver_chain)} approver(s); the last "
                "escalation would have nobody to escalate to"
            )
        return self

    @property
    def needs_human(self) -> bool:
        return self.level is AuthorityLevel.HUMAN

    @property
    def denied(self) -> bool:
        return self.level is AuthorityLevel.DENIED

    @property
    def approver(self) -> str | None:
        return self.approver_chain[0] if self.approver_chain else None


DENY_ALL = ActionAuthority(
    action="*", level=AuthorityLevel.DENIED, source="default:deny", ttl_seconds=1
)
"""The M2 default, and the inversion of M1's.

M1 defaulted unknown actions to AUTO and said so in a comment: right for one gate,
wrong for a resolver. An action nobody wrote a policy for is an action nobody
decided was safe, and the correct answer to a question nobody asked is no.
"""


class ResolvedAuthority(Frozen):
    """Everything an actor may do, frozen at admission."""

    actor_name: str
    role: str | None = None
    department: str | None = None
    actions: dict[str, ActionAuthority] = Field(default_factory=dict)
    tool_grants: frozenset[str] = frozenset()
    """Qualified tool names this actor holds a live grant for, as of admission.
    Revocation is caught by the gateway's ≤30s re-check, not by this set."""
    connections: frozenset[str] = frozenset()
    """Connection names the grants above reference. A tool that needs a credential
    resolves it through one of these, so a connection nobody granted is a tool call
    with nothing to authenticate as."""
    tool_connections: dict[str, str] = Field(default_factory=dict)
    """tool → connection name. Frozen at admission so the gateway does not join
    `tool_grants` to `connections` on every call — the credential *value* is still
    fetched per call and never pinned, which is what T37 is about; this only freezes
    which credential to go and get."""
    connection_credentials: dict[str, str] = Field(default_factory=dict)
    """connection name → credential name. Same reasoning, second hop."""
    approver_budgets: dict[str, int] = Field(default_factory=dict)
    """approver → how many approvals they may be handed in a day.

    Carried on the authority rather than read from `roles` at approval time so that
    an approval created under one policy is *charged* under the same one. Changing a
    role's budget mid-day would otherwise retroactively defer items already queued.
    """
    default_level: AuthorityLevel = AuthorityLevel.DENIED

    def for_action(self, action: str) -> ActionAuthority:
        """Never raises. An unknown action resolves to the default, which denies."""
        found = self.actions.get(action)
        if found is not None:
            return found
        return DENY_ALL.model_copy(update={"action": action, "level": self.default_level})

    def gated_actions(self) -> frozenset[str]:
        return frozenset(a for a, v in self.actions.items() if v.needs_human)

    def credential_for(self, tool: str) -> str | None:
        """Which credential a call to `tool` should authenticate with, if any.

        Two hops — tool → connection → credential — resolved from frozen data. A tool
        with no grant-attached connection returns None, which is correct for the
        majority: most tools need no credential, and inventing one for them would make
        "this tool has no credential" indistinguishable from "this tool's credential is
        missing".
        """
        connection = self.tool_connections.get(tool)
        if connection is None:
            return None
        return self.connection_credentials.get(connection)

    def is_subset_of(self, parent: ResolvedAuthority) -> tuple[bool, str | None]:
        """§3's delegation check: a child's authority must be a subset of its parent's.

        Dormant until M5 — nothing delegates yet — and written now because retrofitting
        it into a delegation implementation means auditing every path that constructs a
        child spec, whereas adding it here is one function and one test.

        "Subset" is defined over what the child can *cause*, not over what its config
        says. A child that needs no approval where the parent needs one is a widening
        even though both entries exist, so the comparison is on strictness rather than
        on presence.
        """
        for action, child in self.actions.items():
            mine = _STRICTNESS[child.level]
            theirs = _STRICTNESS[parent.for_action(action).level]
            if mine < theirs:
                return False, (
                    f"action {action!r}: child is {child.level.value}, parent is "
                    f"{parent.for_action(action).level.value} — a child may not be "
                    "looser than its parent"
                )
        extra = self.tool_grants - parent.tool_grants
        if extra:
            return False, f"child holds tool grants its parent does not: {sorted(extra)}"
        extra_conns = self.connections - parent.connections
        if extra_conns:
            return False, f"child holds connections its parent does not: {sorted(extra_conns)}"
        return True, None


_STRICTNESS = {AuthorityLevel.AUTO: 0, AuthorityLevel.HUMAN: 1, AuthorityLevel.DENIED: 2}
"""Ordering for "may tighten, never loosen". Used by the blast-radius floor and by
the delegation subset check, so both agree on what tighter means."""


BLAST_RADIUS_FLOOR: dict[BlastRadius, AuthorityLevel] = {
    BlastRadius.READ: AuthorityLevel.AUTO,
    BlastRadius.REVERSIBLE: AuthorityLevel.AUTO,
    BlastRadius.IRREVERSIBLE: AuthorityLevel.HUMAN,
}
"""I14 expressed as authority. The last step of the resolution chain: whatever the
policy said, an action performed by an irreversible tool needs a human. A policy
cannot waive it — that is what `allow_listed` on the tool registry is for, and that
is a reviewed act with a diff rather than a config row."""


def apply_floor(resolved: ActionAuthority, floor: AuthorityLevel) -> ActionAuthority:
    """Tighten `resolved` to at least `floor`. Never loosens.

    One case needs more than a level swap. A policy that says `auto` names no approver
    — it did not need one — so raising it to `human` would produce an approval with
    nobody to answer it, which `ActionAuthority` refuses to construct and which would
    be a deadlock if it did not.

    The honest resolution is `denied`: the tool demands a human, the policy provides
    none, and inventing a default approver here would silently route somebody's
    irreversible action to whoever happened to be first in a list. Denied is *stricter*
    than the floor asked for, so it does not violate "may only tighten", and it shows
    up in the denial stream as a policy that needs an approver — which is the actual
    fix.
    """
    if _STRICTNESS[floor] <= _STRICTNESS[resolved.level]:
        return resolved
    if floor is AuthorityLevel.HUMAN and not resolved.approver_chain:
        return resolved.model_copy(
            update={
                "level": AuthorityLevel.DENIED,
                "source": f"{resolved.source}+blast_radius_floor:no_approver",
            }
        )
    return resolved.model_copy(
        update={"level": floor, "source": f"{resolved.source}+blast_radius_floor"}
    )
