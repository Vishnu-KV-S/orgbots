"""Delegation as values.

Everything here is a value or a pure function over values: what a child is asked to
do, what its parent is allowed to spend on it, what the tree has spent already, and —
the part that matters most — *what a child is given*.

It lives in `domain` for the reason `authority` and `memory` do: the three checks that
make delegation safe rather than merely possible are all statements about values, and
a check that needs a database to express is a check somebody will route around. §4's
checks 1, 2, 5 and 6 are all implemented here as functions of two frozen objects.
`DelegationService` does the ones that need a lock, and nothing else.

**`ChildContext` is the security boundary.** M5 §4: *"The child receives: task, output
schema, the facts the parent extracted, a scope subset, a permission subset, budget
headroom, deadline. Never the parent's message history."* That sentence is the type
below, and `extra="forbid"` is what stops a field called `parent_messages` from being
added by someone fixing a quality problem. §9 risk 5 predicts exactly that pressure;
T60 is the guard.
"""

from __future__ import annotations

import uuid
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.authority import ResolvedAuthority
from runtime.domain.enums import ExhaustionPolicy, MemoryScope
from runtime.domain.errors import (
    DelegationCycle,
    DepthExceeded,
    PrivilegeEscalation,
    ScopeEscalation,
)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


# --- limits ---------------------------------------------------------------------------


class DelegationLimits(Frozen):
    """What one actor may do by way of delegation.

    Authored in YAML as M4's `DelegationDoc`, stored on the actor row by `apply`,
    read at admission and frozen into `RunSpec.delegation`. It is deliberately *not*
    on `ActorSpec`: that would change every `spec_hash` in the department and fail
    M4 §8's round-trip, to describe a limit the runtime enforces at admission anyway.
    Same argument, same route, as `RunSpec.memory_scopes`.

    **Every default is the disabled value.** An actor whose configuration says nothing
    about delegation cannot delegate, and an actor that says `enabled: true` and
    nothing else still cannot, because `max_depth` and `max_children` are zero. M4's
    validation refuses that combination with a message saying so; this makes the same
    combination safe if it ever reaches the runtime.
    """

    enabled: bool = False
    max_depth: int = Field(default=0, ge=0)
    """How many generations below *this* run may exist. `depth + 1 <= max_depth`."""
    max_children: int = Field(default=0, ge=0)
    """Live direct children. Check 3."""
    max_live_descendants: int = Field(default=0, ge=0)
    """Live runs anywhere below this one. Check 4, and the one that actually bounds a
    fan-out: `max_children` alone lets three children spawn three each."""

    max_subtree_cost_cents: int = Field(default=0, ge=0)
    max_subtree_llm_calls: int = Field(default=0, ge=0)
    """The two tree-wide ceilings. Zero means *unbounded by this limit* rather than
    "may spend nothing" — the root-run budget pool and `ceilings.max_cost_cents` still
    bound it, and a zero that meant "no spending" would make an actor with delegation
    enabled and no explicit figure unable to do anything at all, which is a footgun
    dressed as a safe default."""

    exhaustion_policy: ExhaustionPolicy = ExhaustionPolicy.DRAIN

    @property
    def bounds_cost(self) -> bool:
        return self.max_subtree_cost_cents > 0

    @property
    def bounds_calls(self) -> bool:
        return self.max_subtree_llm_calls > 0


DELEGATION_OFF = DelegationLimits()
"""The value an actor with no `delegation:` block resolves to. Refuses everything."""


class SubtreeUsage(Frozen):
    """What a run tree has spent and how much of it is still live.

    Read under the pool lock for the two budget checks and read plainly for the two
    fan-out checks — the difference is deliberate and it is the same trade admission
    makes at every other level: a fan-out race over-admits by one child, a budget race
    over-spends, and only one of those is worth a lock.
    """

    cost_cents: int = 0
    llm_calls: int = 0
    live_children: int = 0
    live_descendants: int = 0


# --- the child's world -----------------------------------------------------------------


class TaskSpec(Frozen):
    """What the child is asked to produce.

    `output_schema_ref` is pinned, like every other reference in this system, and for
    the same reason: a child that returns something the parent cannot parse has spent
    the tree's money to produce a rejection.
    """

    input: dict[str, Any] = Field(default_factory=dict)
    output_schema_ref: str | None = None
    title: str = ""
    objective: str = ""


class ChildContext(Frozen):
    """Everything a child run receives from its parent. **The whole list.**

    This type is the isolation claim, stated once, in a place a test can assert
    against. It is simultaneously the token saving and the blast-radius control, and
    it is the thing that will get "optimized" away by somebody adding parent context to
    fix a quality problem (§9 risk 5).

    Three properties hold it:

    *`extra="forbid"`* — a `parent_messages` field cannot be smuggled in at a call site.
    *`facts` is a tuple of strings the parent extracted* — not a transcript, not a
    message list, not a `dict` that could carry one under an innocuous key.
    *T60 asserts the field set itself*, so adding a field is a test failure rather than
    a review someone might wave through.

    The reason it is not merely "we do not pass the history" is that the parent's
    history is *right there* in the parent's context object at the call site. Nothing
    but a type stops it going in.
    """

    task: TaskSpec
    facts: tuple[str, ...] = ()
    """Explicit, extracted by the parent. If the child needs to know something, the
    parent says it in a sentence — which is also the cheapest possible summary and the
    only one whose contents anybody reviewed."""
    memory_scopes: tuple[MemoryScope, ...] = ()
    """A subset of the parent's. Never `None`: an unrestricted child under a restricted
    parent is check 6's failure, and making the widening unrepresentable is better than
    checking for it."""
    budget_headroom_cents: int = Field(default=0, ge=0)
    """Drawn from the root pool, not a private allowance. The child's own
    `ceilings.max_cost_cents` still applies; this is what the *tree* will let it have."""
    deadline_s: float | None = Field(default=None, gt=0)

    def carries_history(self) -> bool:
        """Always False, and it is a method rather than a comment so T60 can call it.

        If a future field made this able to return True, the assertion in T60 that
        walks `model_fields` would have failed first — this exists so that the intent
        is legible at the definition as well as in the test.
        """
        return False


# --- the pure checks (§4, 1/2/5/6) -------------------------------------------------------


def extend_agent_path(path: tuple[str, ...], target: str) -> tuple[str, ...]:
    """Check 1. Append `target`, or refuse because it is already on the path.

    A→B→A is refused, and so is A→B→C→B. The check is on the *path from the root*, not
    on the immediate parent, because a cycle through three actors is still a cycle and
    it is the one nobody notices in review.

    Refusing on the path rather than detecting a loop after the fact is what makes this
    cheap: the path is at most `max_delegation_depth` long, it is carried in the frozen
    spec, and the check is an `in` against a tuple. A cycle detector over live runs
    would be a recursive query on the hot admission path that answers the same question
    later and worse.
    """
    if target in path:
        raise DelegationCycle(
            f"actor {target!r} is already on the delegation path "
            f"{' -> '.join(path)}; a run tree may not revisit an actor"
        )
    return (*path, target)


def check_depth(depth: int, max_delegation_depth: int) -> None:
    """Check 2. The child's depth is `depth + 1`."""
    if depth + 1 > max_delegation_depth:
        raise DepthExceeded(
            f"delegating from depth {depth} would create a run at depth {depth + 1}, "
            f"past the limit of {max_delegation_depth}"
        )


def check_authority_subset(child: ResolvedAuthority, parent: ResolvedAuthority) -> None:
    """Check 5. Static, cheap, and done before anything touches the pool row.

    Delegates to `ResolvedAuthority.is_subset_of`, which M2 wrote and left dormant, so
    there is exactly one definition of what "subset" means — the one the config-time
    check in `runtime.spec.validation` also uses. Two definitions of tighter is how a
    configuration passes review and the runtime disagrees.
    """
    ok, why = child.is_subset_of(parent)
    if not ok:
        raise PrivilegeEscalation(
            f"child {child.actor_name!r} would hold authority its parent "
            f"{parent.actor_name!r} does not: {why}"
        )


def check_scope_subset(
    requested: tuple[MemoryScope, ...] | None,
    parent: tuple[MemoryScope, ...] | None,
    *,
    child_actor: str,
    parent_actor: str,
) -> tuple[MemoryScope, ...] | None:
    """Check 6. Returns the resolved child scopes, or refuses.

    `None` means *every scope the run is entitled to*, which is `RunSpec.memory_scopes`'
    existing meaning and is resolved from the run's own context ids rather than from
    anything a caller supplies. That makes three of the four cases trivial and the
    fourth the only one worth reading:

        parent None, child None      → None. Both unrestricted; the *ids* still differ,
                                       and `readable_scopes` is what keeps them apart.
        parent None, child explicit  → the child's. A narrowing.
        parent explicit, child None  → **refused.** This is the case that looks
                                       innocent and is not: a child asking for its
                                       default under a parent that was deliberately
                                       narrowed is a widening spelled as an omission.
        parent explicit, child explicit → refused unless a subset.

    Note what this does *not* protect against, because it is protected elsewhere: a
    child in another department reading that department's memories. It cannot, and not
    because of this function — `readable_scopes` builds the filter from the child run's
    own ids, so a `DEPARTMENT` scope means the child's department and there is no
    spelling that makes it mean anybody else's. Edge case 81.
    """
    if parent is None:
        return requested
    if requested is None:
        raise ScopeEscalation(
            f"child {child_actor!r} asked for its default memory scopes while its "
            f"parent {parent_actor!r} is restricted to "
            f"{[s.value for s in parent]}; a child may narrow its parent's scopes and "
            "never widen them, and an omission here would widen them"
        )
    extra = [s for s in requested if s not in parent]
    if extra:
        raise ScopeEscalation(
            f"child {child_actor!r} asked for memory scope(s) "
            f"{[s.value for s in extra]} that its parent {parent_actor!r} does not "
            f"hold (parent has {[s.value for s in parent]})"
        )
    return requested


# --- idempotency -------------------------------------------------------------------------


DELEGATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "runtime:delegation")


def child_idempotency_key(
    *,
    parent_run_id: uuid.UUID,
    node: str,
    checkpoint_ns: str,
    ordinal: int,
    target_actor: str,
) -> str:
    """The child's idempotency key, as a deterministic function of where it was asked
    for — never a fresh uuid.

    **This is the M5 equivalent of the duplicate-email bug** (§9 risk 2, T67).
    Spawning a child is a side effect, a replayed node re-executes its body, and a
    `uuid4()` here would produce a second child every time a worker crashed and its run
    was reclaimed. The bug would present as a subtree that costs twice what the ledger
    predicted, days later, with nothing pointing at the line responsible.

    The inputs are exactly `logical_call_id`'s discipline, which is not a coincidence —
    it is the same problem, so it gets the same four components:

    *`parent_run_id`* scopes it to one run, so two runs of the same node are two
    children rather than one shared one.
    *`node` and `checkpoint_ns`* place it in the graph, and the namespace is what
    distinguishes loop iteration 2 from a replay of iteration 1. A cyclic graph that
    omitted it would have every iteration collide on one key and the second child
    would silently receive the first one's result.
    *`ordinal`* orders calls within one node body, reissued from zero on replay by
    `NodeScope`, which is what makes the replay reproduce rather than merely not crash.
    *`target_actor`* is belt and braces: it costs nothing and it means a node that
    fans out to three different actors in one loop cannot alias.
    """
    seed = f"{parent_run_id}|{node}|{checkpoint_ns}|{ordinal}|{target_actor}"
    return f"delegation:{uuid.uuid5(DELEGATION_NAMESPACE, seed)}"


def delegation_row_id(parent_run_id: uuid.UUID, idempotency_key: str) -> uuid.UUID:
    """The `delegations` primary key, derived so the insert is `ON CONFLICT DO NOTHING`
    rather than a read followed by an insert. Same argument as `insert_if_absent`."""
    return uuid.uuid5(DELEGATION_NAMESPACE, f"row|{parent_run_id}|{idempotency_key}")


# --- the request ---------------------------------------------------------------------------


class DelegationOutcome(Frozen):
    """What a completed `delegate()` gives its caller back.

    A value rather than the child's raw output, because a parent needs to know three
    things and only one of them is in the output: whether it succeeded, what it cost
    the tree, and — when it did not — why. A parent that got `{}` for both "the child
    returned nothing" and "the child was cancelled when the subtree ran out of money"
    would summarise the second as the first, which is §9 risk 1 arriving quietly.
    """

    child_run_id: uuid.UUID
    status: str
    """The child's terminal `RunStatus`, by value."""
    output: dict[str, Any] = Field(default_factory=dict)
    cost_cents: int = 0
    reason: str | None = None
    reused: bool = False
    """True when this call found a child it had already spawned — a replay. The parent
    does not normally care, and a test very much does (T67)."""

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCESS"


class DelegationRequest(Frozen):
    """One `delegate()` call, as a value.

    Assembled by the gateway from the node's context and handed to the service, so the
    service's signature does not grow a parameter every time the checks need one more
    piece of information — and so a test can construct the exact request that failed.
    """

    target_actor: str = Field(min_length=1)
    context: ChildContext
    node: str = ""
    checkpoint_ns: str = ""
    ordinal: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.target_actor.strip():
            raise ValueError("target_actor must name an actor")
        return self
