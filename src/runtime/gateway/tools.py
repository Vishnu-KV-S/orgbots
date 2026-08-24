"""Tool gateway.

Every external call a graph makes goes through `ToolGateway.execute()`. That is
I3/I4, and it is enforced mechanically rather than by convention: the import-linter
`gateway-only` contract makes `runtime.graphs` physically unable to import `httpx`,
`boto3` or a provider SDK, so a node that wants to reach the outside has no path
except this one.

The pipeline order is fixed and the order is the design:

    fence → validate → permission → kill switch → authority → ceilings →
    budget reserve → rate limit → repeat guard → EFFECT JOURNAL →
    credentials → execute → validate → fence → kill switch (halt only) →
    scrub → artifact-if-large → trust tag → journal commit → audit → reconcile

Four placements matter more than the rest.

*The journal sits immediately before execution.* Everything cheap and refusable
happens first, so a call that is going to be rejected never leaves an INTENT row
behind. Everything after the journal is either the effect itself or a record of
it.

*The fence check is first, and it is re-checked after execution.* A frozen worker
can thaw between the check and the call; re-checking before committing the result
means a zombie's effect is recorded as orphaned rather than accepted as this run's
output.

*The kill switch comes before authority*, which is a change from M0's documented
order and worth the sentence. Requesting an approval writes a row and charges an
approver's daily budget; doing that for a call a stopped organization was never going
to make would put work in somebody's queue on behalf of a run that is already refused.

*The scrub comes before the artifact write*, not after. A large result is
externalised to the object store, so scrubbing downstream of the size check would
take the secret to a completely different system and leave it there. T38 names four
destinations and every one of them is below this line.

M0 implemented permission (the actor's `allowed_tools`), ceilings, kill switch,
budget, journal, execution, validation, artifacts, trust tagging, audit and
reconcile.

**M1 filled in authority** against a hardcoded dict: one action, one approver.

**M2 fills in the rest, and every step of it costs something.** That is why §9 makes
non-regression an exit criterion and why the three expensive-by-default choices are
all avoided here:

- *Permission* now also checks a live `tool_grants` row, through a ≤30s cache
  (edge case 64), so a grant revoked mid-run is caught without a query per call.
- *Authority* reads the `ResolvedAuthority` **frozen into the RunSpec**, never live
  policy. A run executes under one authority for its whole life, and `spec_hash` is
  the receipt.
- *Kill switch* is a table with a 10s cache and two modes. `drain` refuses new calls
  and lets in-flight ones commit; `halt` also refuses after the effect has fired,
  leaving an INTENT row to reconcile. T40 and T41 are those two sentences.
- *Rate limits* are Redis token buckets over per-connection, per-provider and
  per-actor policies. Refusal releases the budget hold before raising — a rate limit
  that leaked reservations would shrink the pool every time it fired.
- *Credentials* are fetched per call from the encrypted table and never pinned, which
  is what makes a mid-run rotation work (T37). Every value fetched is registered with
  the scrubber, so a provider echoing our key back cannot reach state, a checkpoint, a
  log or an artifact (T38).
- *Audit* now records every **decision**, not just every action — allowed, denied and
  approval_required alike. The rows are buffered and written in one statement, because
  eight checks writing eight rows would be eight round trips on a path that had three.

The order of all this is unchanged from M0 in one respect that matters most: the
journal still sits immediately before execution, so everything cheap and refusable
happens first and a call that is going to be rejected never leaves an INTENT row.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.artifacts.store import ArtifactRef, ArtifactStore
from runtime.budget.service import BudgetService, estimate_tool_cents
from runtime.domain.authority import ActionAuthority
from runtime.domain.context import RunContext
from runtime.domain.enums import (
    ApprovalStatus,
    AuditSeverity,
    BlastRadius,
    EffectStatus,
    GatewayDecision,
    RecoveryPolicy,
    TrustLevel,
)
from runtime.domain.errors import (
    ApprovalDenied,
    ApprovalPending,
    ApprovalRequired,
    AuthorityDenied,
    CeilingExceeded,
    EffectOrphaned,
    GrantRevoked,
    IncoherentEffectPolicy,
    KillSwitchEngaged,
    MissingCredentials,
    StaleFence,
    ToolNotAllowed,
    ToolTimeout,
    UnknownToolError,
    ValidationFailed,
)
from runtime.domain.hashing import args_hash as compute_args_hash
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import ArtifactId, BudgetPoolId, CorrelationId
from runtime.domain.scrub import scrub
from runtime.effects.journal import EffectJournal, EffectMeta, logical_call_id
from runtime.effects.policies import check_entailment, resolve_policy
from runtime.effects.probes import probe_exists
from runtime.effects.recovery import Action, decide
from runtime.gateway.credentials import CredentialBroker
from runtime.gateway.governance import AuditBuffer, PermissionCache
from runtime.gateway.ratelimit import RateLimiter
from runtime.observability.logging import get_logger
from runtime.org.approvals import SUBJECT_TASK, ApprovalService
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("gateway.tools")


class EffectCapabilities(BaseModel):
    """What a tool can actually do about its own side effects.

    These are facts about the provider, not preferences. `check_entailment` uses
    them to reject a recovery policy the tool cannot keep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mutates_external_state: bool
    accepts_idempotency_key: bool = False
    idempotency_key_field: str | None = None
    searchable_marker: bool = False
    marker_field: str | None = None
    marker_search_fn: str | None = None
    compensating_action: str | None = None
    compensation_is_idempotent: bool = False
    max_blast_radius: BlastRadius


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: str
    """Qualified name, e.g. "web.fetch@1"."""
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    ok: bool
    value: dict[str, Any]
    trust: TrustLevel
    logical_call_id: str
    replayed: bool
    artifact: ArtifactRef | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool function gets. Deliberately narrow.

    A tool receives its arguments, its idempotency key and its marker — and no
    session, no lease, no way to reach the database. A tool that could write to
    `runs` could defeat the fence.

    M2 adds the credential, and it arrives here rather than being read by the tool for
    the same reason: a tool that could fetch its own credential could fetch one it was
    not granted. The broker resolves which credential this actor's grant points at, and
    the tool receives the value and no way to ask for another.
    """

    run_id: str
    organization_id: str
    logical_call_id: str
    idempotency_key: str
    marker: str
    attempt: int
    credential: str | None = None
    connection: str | None = None


ToolFn = Callable[[ToolContext, BaseModel], Awaitable[BaseModel]]


class ToolDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    version: int
    args_model: type[BaseModel]
    result_model: type[BaseModel]
    capabilities: EffectCapabilities
    recovery_policy: RecoveryPolicy
    timeout_s: float = 30.0
    max_retries: int | None = None
    """None means "take the blast-radius default". Forced to 0 when IRREVERSIBLE."""
    requires_approval: bool | None = None
    """None means "take the blast-radius default". Forced True when IRREVERSIBLE
    unless allow-listed."""
    audit_severity: AuditSeverity | None = None
    provider: str | None = None
    """Which external provider this tool talks to, e.g. `"serper"`, `"deepseek"`.

    Used by the per-provider rate limit, which is the one of the three scopes a tool
    cannot infer for itself: a connection is per credential and an actor is per caller,
    but "all the tools that hit this vendor" is a fact only the tool knows."""
    authority_action: str | None = None
    """The action name the authority model resolves, e.g. `"publish_external"`.

    Separate from the tool name on purpose: authority is about *what is being done
    to the world*, and two tools can do the same thing. `publish.external@1` and a
    future `publish.social@1` both resolve `publish_external` and share one gate,
    which is what stops "add a new tool" from being a way around an approval."""

    @property
    def qualified(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDef
    fn: ToolFn
    max_retries: int
    requires_approval: bool
    audit_severity: AuditSeverity

    @property
    def qualified(self) -> str:
        return self.definition.qualified

    @property
    def policy(self) -> RecoveryPolicy:
        return self.definition.recovery_policy

    @property
    def blast_radius(self) -> BlastRadius:
        return self.definition.capabilities.max_blast_radius


class ToolRegistry:
    """Tools, and the two checks that gate entry.

    Registration is the last moment at which an incoherent tool can be caught for
    free. After that the only place the incoherence shows up is a production
    incident, so both checks raise rather than warn.
    """

    def __init__(self, *, approval_allow_list: frozenset[str] = frozenset()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._allow_list = approval_allow_list

    def register(self, tool: ToolDef, fn: ToolFn) -> RegisteredTool:
        """Raises `IncoherentEffectPolicy` if `recovery_policy` is not entailed by
        `capabilities`. Applies blast-radius defaults; a tool may tighten them,
        never loosen."""
        caps = tool.capabilities
        check_entailment(
            tool.recovery_policy,
            tool_name=tool.qualified,
            mutates_external_state=caps.mutates_external_state,
            accepts_idempotency_key=caps.accepts_idempotency_key,
            idempotency_key_field=caps.idempotency_key_field,
            searchable_marker=caps.searchable_marker,
            marker_field=caps.marker_field,
            marker_search_fn=caps.marker_search_fn,
        )
        if (
            tool.recovery_policy is RecoveryPolicy.PROBE
            and caps.marker_search_fn
            and not probe_exists(caps.marker_search_fn)
        ):
            raise IncoherentEffectPolicy(
                f"{tool.qualified}: marker_search_fn {caps.marker_search_fn!r} is not "
                "a registered probe"
            )

        resolved = resolve_policy(
            caps.max_blast_radius,
            tool_name=tool.qualified,
            max_retries=tool.max_retries,
            requires_approval=tool.requires_approval,
            audit_severity=tool.audit_severity,
            allow_listed=tool.qualified in self._allow_list,
        )
        registered = RegisteredTool(
            definition=tool,
            fn=fn,
            max_retries=resolved.max_retries,
            requires_approval=resolved.requires_approval,
            audit_severity=resolved.audit_severity,
        )
        self._tools[tool.qualified] = registered
        log.info(
            "tool.registered",
            tool=tool.qualified,
            policy=tool.recovery_policy.value,
            blast_radius=caps.max_blast_radius.value,
            max_retries=resolved.max_retries,
            requires_approval=resolved.requires_approval,
        )
        return registered

    def get(self, qualified: str) -> RegisteredTool:
        try:
            return self._tools[qualified]
        except KeyError as exc:
            raise UnknownToolError(f"no tool registered as {qualified!r}") from exc

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def clear(self) -> None:
        self._tools.clear()


@dataclass
class KillSwitch:
    """A process-local stop. The override, not the mechanism.

    M0 shipped this as *the* kill switch, which meant an operator could only engage it
    by redeploying. M2's real one is `runtime.org.killswitch.KillSwitchService`, backed
    by a table so it can be pulled from a CLI while an incident is happening.

    This stays because there is one case the table cannot serve: a single process that
    must be stopped without touching the organization — a test, a one-off backfill, a
    worker being drained by hand. It is checked *before* the service, so a local stop
    cannot be overridden by the absence of a database row.
    """

    engaged: bool = False
    reason: str = ""
    tools: set[str] = field(default_factory=set)
    """Empty means "everything". Otherwise only these tools are stopped."""

    def check(self, tool: str) -> None:
        if not self.engaged:
            return
        if not self.tools or tool in self.tools:
            raise KillSwitchEngaged(f"kill switch engaged for {tool}: {self.reason}")


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        journal: EffectJournal,
        uow_factory: UnitOfWorkFactory,
        *,
        artifacts: ArtifactStore,
        budget: BudgetService | None = None,
        lease_check: Callable[[Any], Awaitable[None]] | None = None,
        kill_switch: KillSwitch | None = None,
        settings: Settings | None = None,
        kill_switches: KillSwitchService | None = None,
        permissions: PermissionCache | None = None,
        rate_limiter: RateLimiter | None = None,
        credentials: CredentialBroker | None = None,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._uow = uow_factory
        self._artifacts = artifacts
        self._settings = settings or get_settings()
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        self._lease_check = lease_check
        self._kill = kill_switch or KillSwitch()
        # The M2 governance services. All four default to a live implementation
        # except the credential broker, which cannot: it needs a key, and a gateway
        # that invented one would encrypt rows nothing else could read. A gateway
        # built without it simply has no credentials to hand out, and a tool that
        # needs one says so at the point of use.
        self._kill_switches = kill_switches or KillSwitchService(uow_factory)
        self._permissions = permissions or PermissionCache(uow_factory)
        self._rate_limiter = rate_limiter
        self._credentials = credentials

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute(self, ctx: RunContext, call: ToolCall) -> ToolResult:
        """Run one tool call through the whole pipeline.

        The `try/finally` around everything is what makes T43 hold: every gateway
        decision produces an audit row *with a reason*, including the ones that raise.
        A denial that failed to record itself would be invisible to the §9 denial
        review, which is the one place mis-scoped actors are supposed to show up.
        """
        audit = AuditBuffer()
        try:
            return await self._execute(ctx, call, audit)
        finally:
            await audit.flush(self._uow)

    async def _execute(self, ctx: RunContext, call: ToolCall, audit: AuditBuffer) -> ToolResult:
        started = time.perf_counter()
        tool = self._registry.get(call.tool)
        authority = ctx.spec.authority

        # --- fence: before anything else ------------------------------------------
        await self._check_fence(ctx)

        # --- validate --------------------------------------------------------------
        try:
            args = tool.definition.args_model.model_validate(call.args)
        except ValidationError as exc:
            self._decide(
                audit, ctx, tool, GatewayDecision.DENIED, "args_validation", str(exc)[:300]
            )
            raise ValidationFailed(f"{call.tool}: invalid arguments: {exc}") from exc

        # --- permission ------------------------------------------------------------
        # Two checks, and they answer different questions. `allowed_tools` is frozen
        # into the spec and says what this actor was *admitted* to do; the grant is
        # live and says what it may do *now*. A spec cannot be revoked mid-run — that
        # is I11 — so revocation needs the second check, bounded at 30s by the cache
        # (edge case 64).
        if call.tool not in ctx.spec.spec.allowed_tools:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "permission",
                f"not in allowed_tools: {sorted(ctx.spec.spec.allowed_tools)}",
                severity=AuditSeverity.HIGH,
            )
            raise ToolNotAllowed(
                f"actor {ctx.spec.spec.actor_name} may not call {call.tool}; "
                f"allowed: {sorted(ctx.spec.spec.allowed_tools)}"
            )

        if authority.tool_grants and not await self._permissions.holds(
            ctx.organization_id, authority.actor_name, authority.role, call.tool
        ):
            # Two ways to get here, and the denial stream should not conflate them.
            # If the frozen authority listed this tool, the grant existed at admission
            # and has since been withdrawn — governance working. If it did not, the
            # spec's `allowed_tools` and the grant table disagree, which is a seeding
            # bug and a much more alarming line to read at 3am.
            revoked = call.tool in authority.tool_grants
            reason = (
                "grant held at admission is no longer live"
                if revoked
                else "no grant, though the frozen spec allows the tool — "
                "allowed_tools and tool_grants disagree"
            )
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "grant_revoked" if revoked else "grant_missing",
                reason,
                severity=AuditSeverity.HIGH,
            )
            raise GrantRevoked(f"actor {authority.actor_name} may not call {call.tool}: {reason}")

        # --- kill switch -----------------------------------------------------------
        # Local override first: a process being drained by hand must not depend on a
        # database row existing. Audited like every other refusal — T43 is about every
        # denial, and a stop that left no trace would be the one nobody could explain.
        try:
            self._kill.check(call.tool)
        except KillSwitchEngaged:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "kill_switch_local",
                f"process-local stop: {self._kill.reason}",
                severity=AuditSeverity.HIGH,
            )
            raise
        verdict = await self._kill_switches.check(
            ctx.organization_id,
            tool=call.tool,
            actor=authority.actor_name,
            connection=authority.tool_connections.get(call.tool),
        )
        if verdict.stopped:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "kill_switch",
                f"{verdict.mode.value if verdict.mode else '?'} on {verdict.scope}: "
                f"{verdict.reason}",
                severity=AuditSeverity.HIGH,
            )
            verdict.raise_if_stopped(call.tool)

        # --- authority + approval ---------------------------------------------------
        # The gate lives *here* rather than only in the graph that calls the tool,
        # because a graph that forgot its gate node would otherwise publish
        # unapproved. A graph can request the approval early to make the wait
        # visible; it cannot skip it.
        await self._authorise(ctx, tool, call, audit)

        # --- ceilings --------------------------------------------------------------
        if ctx.tool_calls >= ctx.spec.ceilings.max_tool_calls:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "ceiling",
                f"max_tool_calls={ctx.spec.ceilings.max_tool_calls} exhausted",
            )
            raise CeilingExceeded(
                f"run {ctx.run_id} exhausted max_tool_calls={ctx.spec.ceilings.max_tool_calls}"
            )
        ctx.tool_calls += 1

        # --- budget reserve --------------------------------------------------------
        estimate = estimate_tool_cents(call.tool)
        reservation_id = None
        if ctx.spec.budget_pool_id is not None:
            async with self._uow.transaction() as uow:
                # Reserves against every level from this actor's pool to the org root
                # in one locked statement. One round trip per call, not one per level.
                reservation_id = await self._budget.reserve(
                    uow,
                    pool_id=BudgetPoolId(UUID(ctx.spec.budget_pool_id)),
                    run_id=ctx.run_id,
                    amount_cents=estimate,
                )

        # --- rate limit ------------------------------------------------------------
        # After the reservation, per the documented order — so a refusal here has to
        # give the hold back. Skipping that would make every throttled call shrink the
        # pool until the sweeper caught up, which is a budget leak wearing a rate
        # limiter's clothes.
        if self._rate_limiter is not None:
            rate = await self._rate_limiter.check(
                ctx.organization_id,
                actor=authority.actor_name,
                provider=tool.definition.provider,
                connection=authority.tool_connections.get(call.tool),
            )
            if not rate.allowed:
                await self._reconcile(reservation_id, 0)
                self._decide(
                    audit,
                    ctx,
                    tool,
                    GatewayDecision.DENIED,
                    "rate_limit",
                    f"{rate.scope}: {rate.reason}",
                )
                rate.raise_if_limited(call.tool)

        # --- repeat guard / effect journal ----------------------------------------
        ordinal = ctx.scope.next_ordinal()
        arg_digest = compute_args_hash(args.model_dump(mode="json"))
        key = logical_call_id(ctx, ctx.scope.node, ordinal, arg_digest, ctx.scope.checkpoint_ns)
        marker = _marker_for(key)
        idem_key = key

        lookup = await self._journal.begin(
            key,
            EffectMeta(
                tool_name=tool.definition.name,
                tool_version=tool.definition.version,
                recovery_policy=tool.policy,
                blast_radius=tool.blast_radius,
                node=ctx.scope.node,
                checkpoint_ns=ctx.scope.checkpoint_ns,
                ordinal=ordinal,
                args_hash=arg_digest,
                idempotency_key=(
                    idem_key if tool.definition.capabilities.accepts_idempotency_key else None
                ),
                marker=marker if tool.definition.capabilities.searchable_marker else None,
            ),
            ctx,
        )

        decision = await decide(
            lookup,
            policy=tool.policy,
            marker=marker,
            marker_search_fn=tool.definition.capabilities.marker_search_fn,
            tool_name=tool.qualified,
        )

        if decision.action is Action.ORPHAN:
            await self._journal.orphan(key, decision.reason)
            await self._audit(ctx, tool, "orphaned", key, detail={"reason": decision.reason})
            await self._reconcile(reservation_id, 0)
            raise EffectOrphaned(f"{tool.qualified}: {decision.reason}")

        if decision.action is Action.RETURN_RECORDED:
            value = await self._recorded_value(lookup, decision.probe_result)
            if lookup.status is not EffectStatus.COMMITTED:
                await self._journal.commit(key, result_inline=value, marker=marker)
            await self._audit(ctx, tool, "replayed", key, detail={"reason": decision.reason})
            await self._reconcile(reservation_id, 0)
            return ToolResult(
                tool=call.tool,
                ok=True,
                value=value,
                trust=TrustLevel.UNTRUSTED,
                logical_call_id=key,
                replayed=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # --- credentials -----------------------------------------------------------
        # Fetched here, immediately before the call, and never cached. That placement
        # is what makes a mid-run rotation take effect on the next call (T37): a
        # credential resolved at admission and carried through the run would keep using
        # a secret that has since been revoked, which is the failure rotation exists to
        # prevent. The value is registered with the scrubber on the way out of the
        # broker, so the scrub step below can redact it by exact match.
        credential_name = authority.credential_for(call.tool)
        credential = None
        if credential_name is not None:
            if self._credentials is None:
                self._decide(
                    audit,
                    ctx,
                    tool,
                    GatewayDecision.DENIED,
                    "credentials",
                    f"{call.tool} needs credential {credential_name!r} but this gateway "
                    "has no broker configured",
                    severity=AuditSeverity.HIGH,
                )
                await self._reconcile(reservation_id, 0)
                raise MissingCredentials(
                    f"{call.tool} requires credential {credential_name!r} and no "
                    "credential broker is configured on this gateway"
                )
            credential = await self._credentials.fetch(ctx.organization_id, credential_name)

        # --- execute ---------------------------------------------------------------
        tool_ctx = ToolContext(
            run_id=str(ctx.run_id),
            organization_id=str(ctx.organization_id),
            logical_call_id=key,
            idempotency_key=idem_key,
            marker=marker,
            attempt=lookup.row.attempts,
            credential=credential.secret if credential else None,
            connection=authority.tool_connections.get(call.tool),
        )
        try:
            raw = await asyncio.wait_for(tool.fn(tool_ctx, args), timeout=tool.definition.timeout_s)
        except TimeoutError as exc:
            # NOT journal.fail(). A timeout means we do not know whether the effect
            # landed; recording it as FAILED would tell the next replay it is safe
            # to fire again.
            await self._audit(
                ctx, tool, "timeout", key, detail={"timeout_s": tool.definition.timeout_s}
            )
            await self._reconcile(reservation_id, estimate)
            raise ToolTimeout(f"{tool.qualified} exceeded {tool.definition.timeout_s}s") from exc
        except Exception as exc:
            await self._journal.fail(key, f"{type(exc).__name__}: {exc}")
            await self._audit(ctx, tool, "failed", key, detail={"error": str(exc)[:500]})
            await self._reconcile(reservation_id, 0)
            raise

        # --- validate result -------------------------------------------------------
        try:
            result = tool.definition.result_model.model_validate(raw.model_dump(mode="json"))
        except ValidationError as exc:
            await self._journal.fail(key, f"result validation failed: {exc}")
            raise ValidationFailed(f"{call.tool}: invalid result: {exc}") from exc

        payload = result.model_dump(mode="json")

        # --- fence again -----------------------------------------------------------
        # The effect has fired. If we lost the run in the meantime, the effect is
        # not ours to commit — record it as orphaned so a human sees a real event
        # rather than a silent double-execution later.
        try:
            await self._check_fence(ctx)
        except StaleFence:
            await self._journal.orphan(key, "fence advanced during execution")
            await self._reconcile(reservation_id, estimate)
            raise

        # --- kill switch, post-effect (halt only) -----------------------------------
        # The one place the two modes differ. `drain` answers "allowed" here because
        # the effect already happened and committing the record of it is the only way
        # to avoid an orphan (T40). `halt` answers "stopped", and the INTENT row is
        # left standing for reconciliation — which is the trade `halt` exists to make,
        # and why it is not the default (T41).
        halted = await self._kill_switches.check(
            ctx.organization_id,
            tool=call.tool,
            actor=authority.actor_name,
            connection=authority.tool_connections.get(call.tool),
            post_effect=True,
        )
        if halted.stopped:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "kill_switch_post_effect",
                f"halted after the effect fired; INTENT row {key} left for reconciliation",
                severity=AuditSeverity.HIGH,
            )
            await self._reconcile(reservation_id, estimate)
            halted.raise_if_stopped(f"{call.tool} (effect already fired)")

        # --- scrub -------------------------------------------------------------------
        # Before the artifact write, before the journal commit, before the log line,
        # before the value reaches graph state. T38 names those four destinations, and
        # the scrub has to be upstream of all of them — which is here, because
        # everything below this line writes the payload somewhere.
        payload, leaked = scrub(payload)
        if leaked:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.ALLOWED,
                "secret_scrub",
                f"redacted {len(leaked)} secret(s) from the result: {sorted(set(leaked))}",
                severity=AuditSeverity.HIGH,
            )
            log.warning(
                "tool.secret_scrubbed",
                tool=call.tool,
                labels=sorted(set(leaked)),
                **ctx.log_fields(),
            )

        # --- artifact if large -----------------------------------------------------
        artifact: ArtifactRef | None = None
        encoded = _encode(payload)
        if self._artifacts.should_externalise(encoded):
            artifact = await self._artifacts.put(
                encoded,
                organization_id=ctx.organization_id,
                run_id=ctx.run_id,
                kind=f"tool_result:{tool.definition.name}",
                content_type="application/json",
            )
            await self._artifacts.link(
                artifact.artifact_id,
                source_type="effect",
                source_id=key,
                relation="produced",
            )
            payload = {"artifact": artifact.to_json(), "truncated": True}

        # --- trust tag ---------------------------------------------------------------
        # Everything a tool returns came from outside this runtime. It is UNTRUSTED
        # regardless of how safe it looks, and M2 makes that tag structural rather than
        # advisory: `runtime.domain.trust` fences it before it reaches a prompt.
        trust = TrustLevel.UNTRUSTED

        await self._journal.commit(
            key,
            result_ref=artifact.artifact_id if artifact else None,
            result_inline=None if artifact else payload,
            provider_ref=None,
            marker=marker,
        )
        await self._audit(
            ctx,
            tool,
            "executed",
            key,
            detail={
                "artifact": str(artifact.artifact_id) if artifact else None,
                "bytes": len(encoded),
            },
        )
        self._decide(
            audit,
            ctx,
            tool,
            GatewayDecision.ALLOWED,
            "executed",
            None,
            cost_cents=estimate,
        )
        await self._reconcile(reservation_id, estimate)

        return ToolResult(
            tool=call.tool,
            ok=True,
            value=payload,
            trust=trust,
            logical_call_id=key,
            replayed=False,
            artifact=artifact,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # --- helpers -------------------------------------------------------------------

    async def _authorise(
        self, ctx: RunContext, tool: RegisteredTool, call: ToolCall, audit: AuditBuffer
    ) -> None:
        """Enforce the authority frozen into this run's spec.

        Four outcomes now, where M1 had three:

        - the action is AUTO — nothing happens and the call proceeds;
        - the action is **DENIED** — nobody may authorise it, so there is nothing to
          ask and asking would create an approval that can never be granted;
        - a human is required and has granted it — the call proceeds;
        - a human is required and has not — the call raises, and *which* exception it
          raises is the interesting part.

        `ApprovalPending` is not a failure of the work. It means the organization is
        waiting on a person, which is a fact about operating cost that §9 wants counted
        rather than retried. `ApprovalDenied` is terminal: somebody said no, or the
        chain was exhausted under `on_expiry = deny`.

        The authority comes from `ctx.spec`, never from the live tables. A policy edited
        while this run is in flight does not reach it — that is I11, and it is what
        makes `spec_hash` a real answer to "what was this run allowed to do".

        The subject is the **task**, never the run. A run that resumes after a human
        decision is a new run with a new id, so a run-scoped key — including
        `logical_call_id`, which contains the run id by construction — would lose the
        approval a moment after it was granted.
        """
        if not tool.requires_approval:
            return

        action = tool.definition.authority_action
        if action is None:
            # An irreversible tool with nothing to key an approval on. There is no
            # safe way to ask, so the answer is no. This is M0's behaviour, kept for
            # exactly the case M0 was in.
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "authority",
                "tool requires approval but declares no authority_action",
                severity=AuditSeverity.HIGH,
            )
            raise ApprovalRequired(
                f"{call.tool} requires approval but declares no authority_action; "
                "there is no subject to request approval for"
            )

        authority = ctx.spec.authority
        decision: ActionAuthority = authority.for_action(action)

        if decision.denied:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.DENIED,
                "authority",
                f"{action} is denied for {authority.actor_name} (source: {decision.source})",
                severity=AuditSeverity.HIGH,
            )
            raise AuthorityDenied(
                f"{call.tool} performs {action!r}, which actor "
                f"{authority.actor_name!r} may not do under any approval "
                f"(resolved by {decision.source})"
            )

        if not decision.needs_human:
            self._decide(audit, ctx, tool, GatewayDecision.ALLOWED, "authority", f"{action}: auto")
            return

        subject_id = str(ctx.task_id) if ctx.task_id is not None else str(ctx.run_id)
        subject_type = SUBJECT_TASK if ctx.task_id is not None else "run"

        approvals = ApprovalService(self._uow)
        row = await approvals.require(
            organization_id=ctx.organization_id,
            authority=decision,
            subject_type=subject_type,
            subject_id=subject_id,
            detail={
                "tool": call.tool,
                # The *rendered* action, not a summary of it (§5). Built by the runtime
                # from the arguments that will actually be sent, so the approver reads
                # the real recipients and the real amounts rather than the model's
                # account of what it intends to do — which is precisely the thing under
                # review and therefore the last thing that should describe itself.
                "rendered": _render_action(call.tool, call.args),
                "rendered_by": "runtime",
                "blast_radius": tool.blast_radius.value,
            },
            run_id=ctx.run_id,
            actor_name=ctx.spec.spec.actor_name,
            correlation_id=(
                CorrelationId(UUID(ctx.correlation_id)) if ctx.correlation_id else None
            ),
            interrupt_id=_interrupt_id(ctx, action),
            approver_daily_budget=authority.approver_budgets.get(decision.approver or ""),
        )
        if row is None:  # pragma: no cover - needs_human guarantees a row
            return

        await self._audit(
            ctx,
            tool,
            f"authority:{row.status.value.lower()}",
            f"approval:{row.id}",
            detail={"action": action, "subject": subject_id},
        )
        if row.permits:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.ALLOWED,
                "approval",
                f"granted by {row.decided_by}",
                severity=AuditSeverity.HIGH,
            )
            log.info(
                "tool.approved",
                tool=call.tool,
                approval_id=str(row.id),
                decided_by=row.decided_by,
                **ctx.log_fields(),
            )
            return
        if row.status is ApprovalStatus.PENDING:
            self._decide(
                audit,
                ctx,
                tool,
                GatewayDecision.APPROVAL_REQUIRED,
                "approval",
                f"waiting on {row.approver}"
                + ("" if row.queued else " (deferred: approver over daily budget)"),
                severity=AuditSeverity.HIGH,
            )
            raise ApprovalPending(
                f"{call.tool} on {subject_type} {subject_id} is waiting on {row.approver}; "
                f"approval {row.id} expires at {row.expires_at.isoformat()}"
            )
        self._decide(
            audit,
            ctx,
            tool,
            GatewayDecision.DENIED,
            "approval",
            f"{row.status.value} by {row.decided_by or 'ttl'}",
            severity=AuditSeverity.HIGH,
        )
        raise ApprovalDenied(
            f"{call.tool} on {subject_type} {subject_id} was refused "
            f"({row.status.value} by {row.decided_by or 'ttl'})"
        )

    def _decide(
        self,
        audit: AuditBuffer,
        ctx: RunContext,
        tool: RegisteredTool,
        decision: GatewayDecision,
        check_name: str,
        reason: str | None,
        *,
        severity: AuditSeverity | None = None,
        cost_cents: int | None = None,
    ) -> None:
        """Buffer one decision row. Written in a single statement when the call ends.

        Buffering rather than writing is the §9 cost decision: a tool call passes
        roughly eight checks, and eight round trips on a path that had three is how a
        governance milestone regresses the number it was told not to regress.
        """
        audit.add(
            organization_id=ctx.organization_id,
            gateway="tool",
            subject=tool.qualified,
            decision=decision,
            check_name=check_name,
            reason=reason,
            severity=severity or tool.audit_severity,
            run_id=ctx.run_id,
            root_run_id=ctx.root_run_id,
            actor_id=ctx.actor_id,
            actor_name=ctx.spec.spec.actor_name,
            blast_radius=tool.blast_radius.value,
            cost_cents=cost_cents,
            trace_id=ctx.trace_id,
        )

    async def _check_fence(self, ctx: RunContext) -> None:
        if self._lease_check is not None:
            await self._lease_check(ctx.lease)

    async def _recorded_value(
        self, lookup: Any, probe_result: dict[str, object] | None
    ) -> dict[str, Any]:
        """Reconstruct the result of an effect that already happened.

        Preference order: what we stored inline, then the artifact, then whatever
        the probe found. The probe result is last because it describes the
        provider's record of the effect, not necessarily the tool's return value.
        """
        row = lookup.row
        if row.result_inline is not None:
            return dict(row.result_inline)
        if row.result_ref is not None:
            data = await self._artifacts.get(ArtifactId(row.result_ref))
            loaded: dict[str, Any] = json.loads(data.decode("utf-8"))
            return loaded
        if probe_result is not None:
            return dict(probe_result)
        return {}

    async def _reconcile(self, reservation_id: Any, actual_cents: int) -> None:
        if reservation_id is None:
            return
        async with self._uow.transaction() as uow:
            await self._budget.reconcile(uow, reservation_id, actual_cents)

    async def _audit(
        self,
        ctx: RunContext,
        tool: RegisteredTool,
        outcome: str,
        key: str,
        detail: dict[str, Any],
    ) -> None:
        async with self._uow.transaction() as uow:
            await uow.audit.record(
                organization_id=ctx.organization_id,
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
                actor_id=ctx.actor_id,
                fence=int(ctx.fence),
                action=f"tool.{tool.definition.name}",
                target=tool.qualified,
                severity=tool.audit_severity,
                outcome=outcome,
                detail={**detail, "logical_call_id": key},
                trace_id=ctx.trace_id,
            )


RENDER_FIELD_LIMIT = 4_000
"""Per-field cap on a rendered action. Generous on purpose — see `_render_action`."""


def _render_action(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """What the approver actually sees. M2 §5's last bullet, implemented.

    The rule is that the approver reads *the action*, not an account of it: the actual
    recipients, the actual amount, the actual body. So this renders the arguments that
    will really be sent, with two adjustments and no third:

    *Secrets are scrubbed.* An approval request ends up in an audit row, a CLI screen
    and possibly an email. It is a wide surface and it does not need the key.

    *Long fields are capped, loudly.* M1's `_preview` cut every field at 200
    characters, which is fine for a log line and wrong here — a 200-character preview
    of a publish body is exactly the situation where somebody approves the first
    paragraph and ships the rest. The cap is 4,000 and it says how much it hid, so an
    approver who needs the whole thing knows there is a whole thing.

    What this deliberately does not do is summarise. A summary of a publish is the
    model's account of its own intentions, and that is the thing under review.
    """
    scrubbed, _ = scrub(args)
    out: dict[str, Any] = {"_tool": tool}
    for key, value in scrubbed.items():
        text = value if isinstance(value, str) else canonical_json(value)
        if len(text) > RENDER_FIELD_LIMIT:
            hidden = len(text) - RENDER_FIELD_LIMIT
            out[key] = (
                text[:RENDER_FIELD_LIMIT]
                + f"\n\n[{hidden} more characters not shown. This is a truncation, not a "
                "summary — ask for the full value before approving if it matters.]"
            )
        else:
            out[key] = text
    return out


def _interrupt_id(ctx: RunContext, action: str) -> str:
    """Which pause this approval may resume (edge case 28).

    Keyed on the *node and ordinal*, not on the run: two publishes in one run are two
    interrupts, and an approval granted for the first must not resume the second. The
    run id is deliberately absent — a resumed run has a different one, and including it
    would make every token unusable the moment it was needed.
    """
    return f"{ctx.spec.thread_id}:{ctx.scope.node}:{ctx.scope.checkpoint_ns}:{action}"


def _marker_for(key: str) -> str:
    """The stamp a probe searches for.

    Derived from the logical call ID so it is reproduced identically on replay —
    a marker that changed between attempts would make every probe miss.
    """
    return f"lcid:{key[:32]}"


def _encode(payload: dict[str, Any]) -> bytes:
    return canonical_json(payload).encode("utf-8")
