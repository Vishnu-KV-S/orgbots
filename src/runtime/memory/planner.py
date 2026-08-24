"""The read path, and the shadow-mode mechanism.

§7, in order:

    ContextPlanner.plan(run, node, query)
       ├─ scope filter from RunSpec.memory_scopes    ← as a LIST (§3.4)
       ├─ status='active' AND trust != quarantine    ← in the query, not after
       ├─ store.search(top_k=N)
       ├─ rerank (recency x importance x similarity x access decay)
       ├─ take top K, hard token cap
       ├─ write context_trace
       └─ hand the block back for assembly

**The one thing this module must get right is doing nothing.**

§5's phase one is *retrieve, log, do not inject*, and its value is entirely in the
claim that the prompt is byte-identical to M2's. So `plan()` returns a `PlannedContext`
whose `.block` is the empty string whenever injection is off — the retrieval happened,
the trace was written, the tokens that *would* have been spent were counted, and the
model saw nothing. `ck_trace_shadow_injects_nothing` in migration 029 is the database
saying the same thing, and T54 plus `test_m3_shadow.py` are the tests.

Two consequences of that ordering are easy to get wrong and are handled explicitly
below: shadow mode does not touch `access_count` (a measurement that perturbs the thing
it measures is not a measurement), and a retrieval failure is never allowed to fail a
run (retrieval is an enrichment; a department that stops working because its memory is
down has traded a real capability for a hypothetical one).

**Retrieval runs in parallel with other context assembly, never sequentially blocking**
— §7's last line. `plan()` is a coroutine that touches nothing a graph node also
touches, so a node starts it, does its own gathering, and awaits it at assembly time.
`plan_with()` is the helper that makes that the easy way to call it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Sequence
from typing import Any

from runtime.domain.context import RunContext
from runtime.domain.enums import MemoryScope, MemoryStatus
from runtime.domain.ids import MemoryId, OrganizationId, new_context_trace_id
from runtime.domain.memory import (
    InjectionBudget,
    PlannedContext,
    RerankWeights,
    RetrievedMemory,
    ScopeFilter,
    estimate_tokens,
    readable_scopes,
    rerank,
)
from runtime.memory.store import MemoryStore
from runtime.observability.logging import get_logger
from runtime.org.department import company_scope_id, department_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("memory.planner")


class ContextPlanner:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: MemoryStore,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._uow = uow_factory
        self._store = store
        self._settings = settings or get_settings()

    # --- flags ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._settings.memory_enabled

    def injects_for(self, actor_name: str) -> bool:
        """**PR-35, and the only place the injection decision is made.**

        Three gates, all of which must open: the subsystem is on, injection is on, and
        this actor is in the roll-out list (empty list meaning "all"). §13 risk 1 names
        the per-actor flag as the escape hatch, so it is a check rather than a
        deployment: turning memory off for one actor at 3am is an environment variable
        and a worker restart, not a redeploy.
        """
        if not (self._settings.memory_enabled and self._settings.memory_injection_enabled):
            return False
        allowed = {
            a.strip() for a in self._settings.memory_injection_actors.split(",") if a.strip()
        }
        return not allowed or actor_name in allowed

    # --- the plan ------------------------------------------------------------------

    async def plan(
        self,
        ctx: RunContext,
        *,
        node: str,
        query: str,
        call_site: str | None = None,
        top_k: int | None = None,
    ) -> PlannedContext:
        """Retrieve, authorize, rerank, budget, trace. Never raises.

        Every failure path returns an empty plan and logs. The list of things that can
        go wrong — the store is down, the embedder refuses, a scope filter is malformed
        — is a list of things that must degrade the next prompt rather than end the run.
        `MemoryScopeViolation` is caught here too, and that deserves a note: it is a
        *policy* error and policy errors are normally loud, but the loudness that
        matters for this one is the log line and the absent memories, not a failed run.
        A filter that would have over-read has already refused to be built; nothing has
        leaked by the time this catch runs.
        """
        actor_name = ctx.spec.spec.actor_name
        shadow = not self.injects_for(actor_name)
        plan = PlannedContext(query_text=query, shadow_mode=shadow)
        if not self.enabled or not query.strip():
            return plan

        started = time.perf_counter()
        try:
            scope_filter = self._filter_for(ctx)
            candidates = await self._store.search(
                organization_id=OrganizationId(ctx.organization_id),
                query=query,
                scope_values=scope_filter.scope_values(),
                top_k=top_k or self._settings.memory_top_k,
            )
            retrieved = await self._authorize_and_rank(scope_filter, candidates)
            self._inject(plan, retrieved, shadow=shadow)
        except Exception as exc:
            log.warning(
                "memory.retrieval_failed",
                node=node,
                error=f"{type(exc).__name__}: {exc}",
                **ctx.log_fields(),
            )
            return plan

        await self._trace(
            ctx,
            plan,
            node=node,
            call_site=call_site,
            scopes=[str(s) for s in self._scope_keys(ctx)],
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return plan

    def _filter_for(self, ctx: RunContext) -> ScopeFilter:
        """Build the run's filter from the run's own context. §7's first two lines.

        **Nothing here comes from a query.** The organization, the actor, the session
        and the department are all read off `RunContext` and `RunSpec`, which the worker
        loaded from `run_specs`. That is what makes cross-actor leakage (eval 4) a
        structural property rather than a validation: there is no parameter a node could
        pass that names another actor's scope.

        Quarantined memories are admitted for **this run's own actor only** (§6), which
        is the `include_quarantined_for_actor` argument, and it is set from
        `ctx.actor_id` — again, not from anything a caller supplies.
        """
        return ScopeFilter(
            organization_id=ctx.organization_id,
            scopes=self._scope_keys(ctx),
            include_quarantined_for_actor=ctx.actor_id,
            statuses=frozenset({MemoryStatus.ACTIVE, MemoryStatus.QUARANTINED}),
        )

    def _scope_keys(self, ctx: RunContext) -> tuple[Any, ...]:
        org = OrganizationId(ctx.organization_id)
        return readable_scopes(
            session=uuid.UUID(str(ctx.session_id)) if ctx.session_id else None,
            actor=ctx.actor_id,
            department=department_scope_id(org),
            company=company_scope_id(org),
            requested=self._requested_scopes(ctx),
        )

    @staticmethod
    def _requested_scopes(ctx: RunContext) -> Sequence[MemoryScope] | None:
        scopes = getattr(ctx.spec, "memory_scopes", None)
        return tuple(scopes) if scopes else None

    async def _authorize_and_rank(
        self, scope_filter: ScopeFilter, candidates: Sequence[Any]
    ) -> list[RetrievedMemory]:
        """The boundary, then the ordering. In that order, and it matters.

        Authorizing *after* ranking would mean the reranker had seen — and could have
        been influenced by the distribution of — memories the run may not read. That
        would not leak text, but it would leak the existence and shape of another
        actor's store into this run's ordering, which is a weaker version of the same
        failure and one that no test would catch.
        """
        if not candidates:
            return []
        by_id = {c.memory_id: c for c in candidates}
        async with self._uow() as uow:
            records = await uow.memories.authorize(scope_filter, list(by_id))

        floor = self._settings.memory_min_similarity
        permitted = [
            RetrievedMemory(
                memory_id=MemoryId(mid),
                text=by_id[mid].text,
                similarity=by_id[mid].similarity,
                record=record,
            )
            for mid, record in records.items()
            if by_id[mid].similarity >= floor
        ]
        return rerank(permitted, weights=self._weights(), top_k=self._settings.memory_inject_k)

    def _weights(self) -> RerankWeights:
        s = self._settings
        return RerankWeights(
            similarity=s.memory_rerank_similarity,
            importance=s.memory_rerank_importance,
            recency=s.memory_rerank_recency,
            access=s.memory_rerank_access,
            half_life_days=s.memory_rerank_half_life_days,
        )

    def _inject(
        self, plan: PlannedContext, retrieved: Sequence[RetrievedMemory], *, shadow: bool
    ) -> None:
        """Apply the token cap and mark what would have been injected.

        **The counting is identical in both modes.** In shadow mode the selected set is
        computed exactly as it would be live, its tokens are totalled into
        `would_have_injected_tokens`, and then nothing is put in the prompt. That is
        what makes eval 8 executable during phase one — §11: *"Measure it in shadow mode
        (compute the tokens you would have injected), then set a budget."* A shadow path
        that skipped the selection would have left eval 8 with nothing to measure until
        the day injection went live, which is the day it is too late to set a budget.
        """
        budget = InjectionBudget(
            max_memories=self._settings.memory_inject_k,
            max_tokens=self._settings.memory_max_injected_tokens,
        )
        used = 0
        chosen: list[RetrievedMemory] = []
        for candidate in retrieved:
            body = candidate.text[: budget.max_chars_per_memory]
            cost = estimate_tokens(body)
            if not budget.fits(used, cost, len(chosen)):
                break
            used += cost
            chosen.append(
                RetrievedMemory(
                    memory_id=candidate.memory_id,
                    text=body,
                    similarity=candidate.similarity,
                    record=candidate.record,
                    rank=candidate.rank,
                    score=candidate.score,
                    injected=not shadow,
                )
            )

        plan.retrieved = [
            RetrievedMemory(
                memory_id=c.memory_id,
                text=c.text,
                similarity=c.similarity,
                record=c.record,
                rank=c.rank,
                score=c.score,
                injected=(not shadow) and any(x.memory_id == c.memory_id for x in chosen),
            )
            for c in retrieved
        ]
        plan.injected = [] if shadow else chosen
        plan.injected_tokens = 0 if shadow else used
        plan.would_have_injected_tokens = used

    async def _trace(
        self,
        ctx: RunContext,
        plan: PlannedContext,
        *,
        node: str,
        call_site: str | None,
        scopes: list[str],
        latency_ms: float,
    ) -> None:
        """One row per retrieval, shadow or live (T54). Its own transaction.

        Separate from anything the run is doing, because a trace is evidence about a
        prompt and must survive the run failing afterwards — a shadow-mode window whose
        traces disappeared whenever the run went on to fail would be a sample biased
        toward success, which is precisely the wrong bias for grading retrieval quality.

        **`access_count` moves only when something was actually injected.** In shadow
        mode nothing was, so nothing is touched: the store must not learn from being
        observed, or two weeks of shadow retrieval would train the rerank's access term
        on memories no model ever saw.
        """
        try:
            async with self._uow.transaction() as uow:
                await uow.traces.write(
                    trace_id=new_context_trace_id(),
                    organization_id=OrganizationId(ctx.organization_id),
                    run_id=ctx.run_id,
                    task_id=uuid.UUID(str(ctx.task_id)) if ctx.task_id else None,
                    actor_name=ctx.spec.spec.actor_name,
                    node=node,
                    call_site=call_site,
                    query_text=plan.query_text,
                    retrieved=[m.trace_entry() for m in plan.retrieved],
                    injected_count=len(plan.injected),
                    injected_tokens=plan.injected_tokens,
                    would_have_injected_tokens=plan.would_have_injected_tokens,
                    scopes=scopes,
                    embedding_version=getattr(
                        getattr(self._store, "embeddings", None), "embedding_version", None
                    ),
                    latency_ms=latency_ms,
                    shadow_mode=plan.shadow_mode,
                )
                if plan.injected:
                    ids = [m.memory_id for m in plan.injected]
                    await uow.memories.touch_access(ids)
                    for memory_id in ids:
                        await uow.memories.audit(
                            memory_id=memory_id,
                            organization_id=OrganizationId(ctx.organization_id),
                            event="ACCESS",
                            actor_name=ctx.spec.spec.actor_name,
                            run_id=ctx.run_id,
                            detail={"node": node},
                        )
        except Exception as exc:
            # A trace that cannot be written is a measurement lost, not a run lost.
            log.warning("memory.trace_failed", node=node, error=str(exc), **ctx.log_fields())


async def plan_with[T](
    planner: ContextPlanner,
    ctx: RunContext,
    *,
    node: str,
    query: str,
    alongside: Awaitable[T],
    call_site: str | None = None,
) -> tuple[PlannedContext, T]:
    """Run retrieval concurrently with whatever else the node was going to fetch.

    §7's *"Retrieval runs in parallel with other context assembly, never sequentially
    blocking"*, made the easy thing to write. A node gathers its session, its messages
    and its artifacts in `alongside`, and the memory round trip disappears into that
    wait instead of adding to it.

    `return_exceptions` is deliberately **not** set: `plan()` already swallows its own
    failures and returns an empty plan, so an exception surfacing here came from
    `alongside` — the node's real work — and must not be quietly converted into a
    value the node then treats as data.
    """
    return await asyncio.gather(
        planner.plan(ctx, node=node, query=query, call_site=call_site), alongside
    )
