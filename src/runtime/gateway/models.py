"""Model gateway.

Three refusals, in this order, before any provider is touched:

1. **No `work_class`** — I12. The parameter is keyword-only and required, so the
   type checker catches most of it; the runtime check catches the rest, because
   `work_class` is what selects the profile, what the usage ledger aggregates on,
   and what makes "this run spent $4 on classification" answerable.

2. **`max_llm_calls == 0`** — a deterministic worker is deterministic because the
   gateway refuses, not because the handler is well behaved. T10.

3. **HYBRID with an undeclared `call_site`** — for when HYBRID exists. Today the
   compiler refuses HYBRID outright, so this branch is unreachable through normal
   admission; it is written now so that turning HYBRID on later is a change to the
   compiler and not a new enforcement point that someone has to remember to add.

M0 shipped one provider: an echo provider with a deterministic response, because
what M0 proved was that the *accounting* around a model call is correct.

M1 adds a real one (`runtime.gateway.providers.deepseek_provider`, which is the
Messages client in `anthropic_provider` pointed at DeepSeek). The echo
provider stays and stays the default, because every correctness test in the suite
depends on a model whose output does not vary between the attempt that crashed and
the attempt that resumes. A chaos test against a real model would be measuring the
model's variance rather than the runtime's exactly-once guarantee.

**M2 governs this gateway too**, and it is worth saying why, because a model call is
not an "external effect" in the sense the effect journal cares about — it mutates
nothing, so it needs no INTENT row. What it does do is *spend money* and *talk to a
provider with a quota*, and those are exactly the two things M2 exists to bound. So a
model call now passes a kill switch (a runaway loop is stopped by the same lever as a
runaway publish), a per-provider and per-actor rate limit, and writes a decision row
like every other gateway call — which is what makes the denial stream complete rather
than tool-shaped.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runtime.budget.service import BudgetService
from runtime.domain.context import RunContext
from runtime.domain.enums import (
    ActorKind,
    AuditSeverity,
    GatewayDecision,
    TrustLevel,
    WorkClass,
)
from runtime.domain.errors import (
    CeilingExceeded,
    MissingWorkClass,
    ModelCallNotAllowed,
)
from runtime.domain.ids import BudgetPoolId, OrganizationId, RunId
from runtime.domain.scrub import scrub_text
from runtime.domain.specs import ModelProfile
from runtime.gateway.credentials import CredentialBroker
from runtime.gateway.governance import AuditBuffer
from runtime.gateway.ratelimit import RateLimiter
from runtime.observability.logging import get_logger
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("gateway.models")


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    system: str | None = None
    max_output_tokens: int | None = None
    metadata: dict[str, Any] = {}
    """Provider hints that are not part of the contract: `json_schema` for a
    structured response, and `work_class`, which the gateway fills in itself so a
    call site cannot declare one class for accounting and imply another for spend."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    trust: TrustLevel = TrustLevel.UNTRUSTED
    """A completion is text from outside the runtime. It is never trusted input."""
    duration_ms: float = 0.0


class Provider(Protocol):
    name: str

    async def complete(self, profile: ModelProfile, req: ModelRequest) -> ModelResponse: ...


@dataclass
class EchoProvider:
    """M0's only provider.

    Deterministic on purpose: a run's output must not change between the attempt
    that crashed and the attempt that resumes, or the chaos tests would be testing
    the provider's variance rather than the runtime's correctness.
    """

    name: str = "fake"
    calls: list[ModelRequest] = field(default_factory=list)

    async def complete(self, profile: ModelProfile, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        text = f"echo: {req.prompt}"
        input_tokens = max(1, len(req.prompt) // 4)
        output_tokens = max(1, len(text) // 4)
        cost = (
            input_tokens * profile.input_cents_per_mtok
            + output_tokens * profile.output_cents_per_mtok
        ) // 1_000_000
        return ModelResponse(
            text=text,
            provider=profile.provider,
            model=profile.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=cost,
        )


class ModelGateway:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        providers: dict[str, Provider] | None = None,
        budget: BudgetService | None = None,
        settings: Settings | None = None,
        lease_check: Any = None,
        kill_switches: KillSwitchService | None = None,
        rate_limiter: RateLimiter | None = None,
        credentials: CredentialBroker | None = None,
        provider_credentials: Mapping[str, str] | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._providers: dict[str, Provider] = providers or {"fake": EchoProvider()}
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        self._lease_check = lease_check
        self._kill_switches = kill_switches or KillSwitchService(uow_factory)
        self._rate_limiter = rate_limiter
        self._credentials = credentials
        # provider name -> credential name in the M2 store. Only providers listed here
        # are fed from the table; anything else keeps whatever the provider resolved
        # for itself, which for the echo provider is nothing at all.
        self._provider_credentials = dict(provider_credentials or DEFAULT_PROVIDER_CREDENTIALS)

    async def complete(
        self,
        ctx: RunContext,
        req: ModelRequest,
        *,
        work_class: WorkClass,
        call_site: str,
    ) -> ModelResponse:
        """Rejects: missing `work_class` (I12); any call when
        `ctx.spec.ceilings.max_llm_calls == 0` (deterministic workers); for HYBRID,
        any `call_site` not in `allowed_model_call_sites`; and — M2 — a kill switch or
        a rate limit."""
        audit = AuditBuffer()
        try:
            return await self._complete(
                ctx, req, work_class=work_class, call_site=call_site, audit=audit
            )
        finally:
            await audit.flush(self._uow)

    async def _complete(
        self,
        ctx: RunContext,
        req: ModelRequest,
        *,
        work_class: WorkClass,
        call_site: str,
        audit: AuditBuffer,
    ) -> ModelResponse:
        if self._lease_check is not None:
            await self._lease_check(ctx.lease)

        # I12. `work_class` is keyword-only and typed, so mypy catches most of
        # this; the isinstance check catches a value that arrived from untyped code
        # — a graph node reading a work class out of its own state, for instance.
        if not isinstance(work_class, WorkClass):
            raise MissingWorkClass(
                f"{call_site}: every model call must declare a work_class (I12), got {work_class!r}"
            )
        if not call_site:
            raise MissingWorkClass(f"model call from run {ctx.run_id} has no call_site")

        ceilings = ctx.spec.ceilings
        if ceilings.max_llm_calls == 0:
            raise ModelCallNotAllowed(
                f"actor {ctx.spec.spec.actor_name} is a "
                f"{ctx.spec.spec.kind.value} with max_llm_calls=0; "
                f"the call from {call_site!r} is refused at the gateway"
            )

        if ctx.spec.spec.kind is ActorKind.HYBRID:
            allowed = ctx.spec.spec.allowed_model_call_sites
            if allowed is None or call_site not in allowed:
                raise ModelCallNotAllowed(
                    f"HYBRID actor {ctx.spec.spec.actor_name} has not declared "
                    f"call site {call_site!r}"
                )

        if ctx.llm_calls >= ceilings.max_llm_calls:
            raise CeilingExceeded(
                f"run {ctx.run_id} exhausted max_llm_calls={ceilings.max_llm_calls}"
            )
        ctx.llm_calls += 1

        profile = ctx.spec.spec.model_profiles.for_work_class(work_class)
        provider = self._providers.get(profile.provider)
        if provider is None:
            raise ModelCallNotAllowed(f"provider {profile.provider!r} is not configured")

        # --- kill switch -----------------------------------------------------------
        # A model call mutates nothing, so it needs no post-effect check: there is no
        # in-flight effect to orphan, and `drain` and `halt` therefore mean the same
        # thing here. One check, before the call.
        actor_name = ctx.spec.spec.actor_name
        stop = await self._kill_switches.check(ctx.organization_id, actor=actor_name)
        if stop.stopped:
            self._decide(
                audit,
                ctx,
                profile,
                GatewayDecision.DENIED,
                "kill_switch",
                f"{stop.mode.value if stop.mode else '?'} on {stop.scope}: {stop.reason}",
                work_class,
                call_site,
                severity=AuditSeverity.HIGH,
            )
            stop.raise_if_stopped(f"model call from {call_site}")

        # --- rate limit ------------------------------------------------------------
        # Before the reservation here, unlike the tool gateway. A model call has no
        # journal and no effect to protect, so there is nothing gained by holding
        # budget across the check — and not taking the hold means not having to give
        # it back.
        if self._rate_limiter is not None:
            rate = await self._rate_limiter.check(
                ctx.organization_id, actor=actor_name, provider=profile.provider
            )
            if not rate.allowed:
                self._decide(
                    audit,
                    ctx,
                    profile,
                    GatewayDecision.DENIED,
                    "rate_limit",
                    f"{rate.scope}: {rate.reason}",
                    work_class,
                    call_site,
                )
                rate.raise_if_limited(f"model call from {call_site}")

        # --- credentials -----------------------------------------------------------
        # M2 moves credentials out of the environment, and a model provider is a
        # credential holder like any other. Fetched per call from the encrypted table,
        # so a rotation lands on the next call; the provider rebuilds its client only
        # when the fingerprint changes, which keeps prompt caching's stable prefix
        # intact between rotations.
        #
        # A provider with no row in the table keeps whatever it resolved for itself —
        # a `DEEPSEEK_API_KEY` from the environment, say. That is the
        # development path and it is deliberately still open: requiring a credentials
        # table to run the test suite would be a governance mechanism that made the
        # thing harder to work on, which is how governance gets switched off.
        await self._apply_credential(ctx, profile, provider)

        estimate = _estimate_cents(profile, req)
        reservation_id = None
        pool_id = (
            BudgetPoolId(UUID(ctx.spec.budget_pool_id))
            if ctx.spec.budget_pool_id is not None
            else None
        )
        if pool_id is not None:
            async with self._uow.transaction() as uow:
                reservation_id = await self._budget.reserve(
                    uow, pool_id=pool_id, run_id=ctx.run_id, amount_cents=estimate
                )

        started = time.perf_counter()
        # The provider needs the work class to choose an effort level, and having
        # the gateway put it there means a call site cannot declare one class for
        # accounting and imply another for spend.
        outbound = req.model_copy(
            update={
                "metadata": {
                    **req.metadata,
                    "work_class": work_class.value,
                    "server_tools": self._server_tools(ctx),
                }
            }
        )
        try:
            response = await provider.complete(profile, outbound)
        except Exception:
            if reservation_id is not None:
                async with self._uow.transaction() as uow:
                    await self._budget.release(uow, reservation_id)
            raise

        elapsed = (time.perf_counter() - started) * 1000
        async with self._uow.transaction() as uow:
            if reservation_id is not None:
                await self._budget.reconcile(uow, reservation_id, response.cost_cents)
            await uow.budget.record_usage(
                organization_id=ctx.organization_id,
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
                pool_id=pool_id,
                reservation_id=reservation_id,
                kind="model",
                work_class=work_class.value,
                call_site=call_site,
                provider=response.provider,
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_cents=response.cost_cents,
            )

        self._decide(
            audit,
            ctx,
            profile,
            GatewayDecision.ALLOWED,
            "completed",
            None,
            work_class,
            call_site,
            cost_cents=response.cost_cents,
        )
        log.info(
            "model.completed",
            work_class=work_class.value,
            call_site=call_site,
            model=response.model,
            cost_cents=response.cost_cents,
            **ctx.log_fields(),
        )

        # A completion is text from outside. It can contain a credential — the model
        # was shown one in an error message, or repeated one out of a fetched page —
        # so it is scrubbed before it reaches graph state, a checkpoint or a log. T38
        # names four destinations and this is the doorway to all four.
        text, leaked = scrub_text(response.text)
        if leaked:
            log.warning(
                "model.secret_scrubbed",
                call_site=call_site,
                labels=sorted(set(leaked)),
                **ctx.log_fields(),
            )
        return ModelResponse(
            text=text,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_cents=response.cost_cents,
            duration_ms=elapsed,
        )

    async def complete_detached(
        self,
        organization_id: OrganizationId,
        req: ModelRequest,
        *,
        profile: ModelProfile,
        work_class: WorkClass,
        call_site: str,
        run_id: RunId | None = None,
        pool_id: BudgetPoolId | None = None,
        actor_name: str | None = None,
    ) -> ModelResponse:
        """A model call made outside any run. **M3's memory worker is the only caller.**

        The memory write path runs after a run has finished (§6: *"never the hot
        path"*). There is no lease, no fence, no frozen spec and no `RunContext`, and
        the two ways to pretend otherwise are both worse than this method: fabricating a
        `RunSpec` in the worker puts a spec in the system that describes a run nobody
        executed, and letting the worker hold a provider client of its own is the
        ungoverned path I3 exists to close.

        **What still applies:** the kill switch, the rate limit, the budget pool, the
        usage ledger, credentials from the M2 store, secret scrubbing, and one audit
        decision row. Everything M2 added, in the same order.

        **What does not, and why that is survivable:** the per-run ceilings. There is no
        run, so `max_llm_calls` has nothing to count against. A runaway consolidation
        loop is therefore bounded by the *pool* rather than by a ceiling — which is a
        weaker bound, and it is the reason `MemoryWorker` batches with an explicit size
        rather than draining until empty. The kill switch remains the stop.

        The profile is passed rather than resolved, because resolution reads a spec and
        there is not one. `department.MEMORY_PROFILE` is where the standing choice
        lives, and passing it explicitly keeps "which model writes our memories" a value
        somebody chose rather than a lookup that defaulted.
        """
        audit = AuditBuffer()
        try:
            return await self._complete_detached(
                organization_id,
                req,
                profile=profile,
                work_class=work_class,
                call_site=call_site,
                run_id=run_id,
                pool_id=pool_id,
                actor_name=actor_name,
                audit=audit,
            )
        finally:
            await audit.flush(self._uow)

    async def _complete_detached(
        self,
        organization_id: OrganizationId,
        req: ModelRequest,
        *,
        profile: ModelProfile,
        work_class: WorkClass,
        call_site: str,
        run_id: RunId | None,
        pool_id: BudgetPoolId | None,
        actor_name: str | None,
        audit: AuditBuffer,
    ) -> ModelResponse:
        if not isinstance(work_class, WorkClass):
            raise MissingWorkClass(f"{call_site}: detached model call has no work_class (I12)")
        provider = self._providers.get(profile.provider)
        if provider is None:
            raise ModelCallNotAllowed(f"provider {profile.provider!r} is not configured")

        stop = await self._kill_switches.check(organization_id, actor=actor_name)
        if stop.stopped:
            self._decide_detached(
                audit,
                organization_id,
                profile,
                GatewayDecision.DENIED,
                "kill_switch",
                f"{stop.mode.value if stop.mode else '?'} on {stop.scope}: {stop.reason}",
                work_class,
                call_site,
                run_id,
                actor_name,
                severity=AuditSeverity.HIGH,
            )
            stop.raise_if_stopped(f"detached model call from {call_site}")

        if self._rate_limiter is not None:
            rate = await self._rate_limiter.check(
                organization_id, actor=actor_name, provider=profile.provider
            )
            if not rate.allowed:
                self._decide_detached(
                    audit,
                    organization_id,
                    profile,
                    GatewayDecision.DENIED,
                    "rate_limit",
                    f"{rate.scope}: {rate.reason}",
                    work_class,
                    call_site,
                    run_id,
                    actor_name,
                )
                rate.raise_if_limited(f"detached model call from {call_site}")

        await self._apply_detached_credential(organization_id, profile, provider)

        estimate = _estimate_cents(profile, req)
        reservation_id = None
        if pool_id is not None and run_id is not None:
            async with self._uow.transaction() as uow:
                reservation_id = await self._budget.reserve(
                    uow, pool_id=pool_id, run_id=run_id, amount_cents=estimate
                )

        started = time.perf_counter()
        outbound = req.model_copy(
            update={
                "metadata": {
                    **req.metadata,
                    "work_class": work_class.value,
                    "server_tools": (),
                }
            }
        )
        try:
            response = await provider.complete(profile, outbound)
        except Exception:
            if reservation_id is not None:
                async with self._uow.transaction() as uow:
                    await self._budget.release(uow, reservation_id)
            raise
        elapsed = (time.perf_counter() - started) * 1000

        async with self._uow.transaction() as uow:
            if reservation_id is not None:
                await self._budget.reconcile(uow, reservation_id, response.cost_cents)
            # `usage_ledger.run_id` is NOT NULL, so a call with nothing to attribute to
            # cannot go in it. Rather than fabricate a run, the row is skipped and the
            # cost is carried by the audit decision row below, which permits a null run
            # and already has a `cost_cents` column. §13 risk 4's dashboard question —
            # "what is consolidation costing" — is therefore answerable from
            # `audit_logs` filtered on `work_class = 'memory'` whether or not the call
            # had a run. The log line names the gap so it is not discovered as a
            # discrepancy between two totals.
            if run_id is not None:
                await uow.budget.record_usage(
                    organization_id=organization_id,
                    run_id=run_id,
                    root_run_id=run_id,
                    pool_id=pool_id,
                    reservation_id=reservation_id,
                    kind="model",
                    work_class=work_class.value,
                    call_site=call_site,
                    provider=response.provider,
                    model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_cents=response.cost_cents,
                )
            else:
                log.info(
                    "model.detached_unattributed",
                    call_site=call_site,
                    work_class=work_class.value,
                    cost_cents=response.cost_cents,
                )

        self._decide_detached(
            audit,
            organization_id,
            profile,
            GatewayDecision.ALLOWED,
            "completed",
            None,
            work_class,
            call_site,
            run_id,
            actor_name,
            cost_cents=response.cost_cents,
        )
        text, leaked = scrub_text(response.text)
        if leaked:
            log.warning("model.secret_scrubbed", call_site=call_site, labels=sorted(set(leaked)))
        return ModelResponse(
            text=text,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_cents=response.cost_cents,
            duration_ms=elapsed,
        )

    async def _apply_detached_credential(
        self, organization_id: OrganizationId, profile: ModelProfile, provider: Provider
    ) -> None:
        name = self._provider_credentials.get(profile.provider)
        applier = getattr(provider, "use_credential", None)
        if name is None or self._credentials is None or applier is None:
            return
        credential = await self._credentials.try_fetch_optional(organization_id, name)
        if credential is None:
            return
        applier(credential.secret, fingerprint=credential.fingerprint)

    def _decide_detached(
        self,
        audit: AuditBuffer,
        organization_id: OrganizationId,
        profile: ModelProfile,
        decision: GatewayDecision,
        check_name: str,
        reason: str | None,
        work_class: WorkClass,
        call_site: str,
        run_id: RunId | None,
        actor_name: str | None,
        *,
        severity: AuditSeverity = AuditSeverity.LOW,
        cost_cents: int | None = None,
    ) -> None:
        audit.add(
            organization_id=organization_id,
            gateway="model",
            subject=f"{profile.provider}/{profile.model}",
            decision=decision,
            check_name=check_name,
            reason=reason,
            severity=severity,
            run_id=run_id,
            root_run_id=run_id,
            actor_name=actor_name,
            cost_cents=cost_cents,
            detail={"work_class": work_class.value, "call_site": call_site, "detached": True},
        )

    @staticmethod
    def _server_tools(ctx: RunContext) -> tuple[str, ...]:
        """Which provider-side tools this actor may be offered.

        The intersection of two answers that are deliberately kept separate: the spec's
        `allowed_tools` — what the actor was *admitted* with, frozen at admission — and
        the authority's `tool_grants` — what it may use *now*. `ToolGateway` checks both
        for a front-door call, so a side door that checked only one would be the looser
        of the two, and the looser one is the stale one.

        A spec with no resolved authority is an M0/M1 spec, and there `allowed_tools` is
        the whole answer; there is no grant table to disagree with it.
        """
        spec = ctx.spec.spec
        allowed = set(spec.allowed_tools) & SERVER_TOOL_GRANTS
        if spec.authority is not None:
            allowed &= set(spec.authority.tool_grants)
        return tuple(sorted(allowed))

    async def _apply_credential(
        self, ctx: RunContext, profile: ModelProfile, provider: Provider
    ) -> None:
        """Hand the provider its credential from the store, if there is one to hand.

        Silent when the provider is not credential-backed, when no broker is
        configured, or when the store has no row — all three are the development path.
        `MissingCredentials` from the broker is *not* swallowed: a row that exists and
        will not decrypt is a deployment fault, and the difference between "no
        credential configured" and "the configured credential is broken" is the whole
        value of the error.
        """
        name = self._provider_credentials.get(profile.provider)
        applier = getattr(provider, "use_credential", None)
        if name is None or self._credentials is None or applier is None:
            return
        credential = await self._credentials.try_fetch_optional(ctx.organization_id, name)
        if credential is None:
            return
        applier(credential.secret, fingerprint=credential.fingerprint)

    def _decide(
        self,
        audit: AuditBuffer,
        ctx: RunContext,
        profile: ModelProfile,
        decision: GatewayDecision,
        check_name: str,
        reason: str | None,
        work_class: WorkClass,
        call_site: str,
        *,
        severity: AuditSeverity = AuditSeverity.LOW,
        cost_cents: int | None = None,
    ) -> None:
        """One decision row for a model call.

        `severity` defaults to LOW because an allowed model call is the highest-volume
        row in the table and nobody reads it individually — it exists to be the
        denominator that makes a denial rate mean something. Denials pass HIGH.
        """
        audit.add(
            organization_id=ctx.organization_id,
            gateway="model",
            subject=f"{profile.provider}/{profile.model}",
            decision=decision,
            check_name=check_name,
            reason=reason,
            severity=severity,
            run_id=ctx.run_id,
            root_run_id=ctx.root_run_id,
            actor_id=ctx.actor_id,
            actor_name=ctx.spec.spec.actor_name,
            cost_cents=cost_cents,
            trace_id=ctx.trace_id,
            detail={"work_class": work_class.value, "call_site": call_site},
        )


DEFAULT_PROVIDER_CREDENTIALS: dict[str, str] = {
    "deepseek": "deepseek_api_key",
}
"""Which stored credential feeds which provider. `fake` is absent because it has
nothing to authenticate as, which is also why the test suite needs no credentials
table. `anthropic` is absent for a different reason: it is no longer registered in
`build_providers`, and a mapping for a provider nobody can name would be a row
promising a route that does not exist."""

SERVER_TOOL_GRANTS: frozenset[str] = frozenset({"web.search@1"})
"""Grants that unlock a *provider-side* tool.

A model provider that searches on its own server does work `ToolGateway` never sees:
no journal row, no per-connection rate limit, no approval. The only defensible rule
is that it may do nothing the actor could not have done through the front door, so
the offer is gated on the same grant the front door checks. Adding a name here is
adding an ungoverned path, and should be as hard as it looks."""


def _estimate_cents(profile: ModelProfile, req: ModelRequest) -> int:
    """Reserve against the worst case, not the expected case.

    Under-reserving lets a run start a call it cannot pay for, which is the failure
    the reservation exists to prevent. The over-reservation is returned at
    reconcile a few hundred milliseconds later.
    """
    input_tokens = max(1, len(req.prompt) // 4)
    output_tokens = req.max_output_tokens or profile.max_output_tokens
    return (
        input_tokens * profile.input_cents_per_mtok + output_tokens * profile.output_cents_per_mtok
    ) // 1_000_000
