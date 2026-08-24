"""The nine evals. §11.

    | Eval | Method                                   | Bar                  |
    |------|------------------------------------------|----------------------|
    | 1 Fact recall            | golden facts.jsonl        | [CHOSEN] ≥80%        |
    | 2 Retrieval precision    | golden queries.jsonl      | [CHOSEN] ≥60%        |
    | 3 Irrelevant rate        | graded traces             | [CHOSEN] ≤5%         |
    | 4 Cross-actor leakage    | T49                       | 0 — hard gate        |
    | 5 Cross-scope leakage    | T48                       | 0 — hard gate        |
    | 6 Contradiction handling | contradictions.jsonl      | 100%                 |
    | 7 Summary fidelity       | key facts surviving       | [CHOSEN] ≥90%        |
    | 8 Token effect           | vs M2 baseline            | measured, then budgeted |
    | 9 Quarantine containment | T50, T52                  | 0 escapes — hard gate|

Three properties of this module are what make it worth having rather than a script.

**Measurements, not pass/fail — except where they are.** Evals 4, 5 and 9 are
*isolation* properties and their bar is zero, so they return a hard verdict. The rest
return a number with the bar attached, and the bar is a value that was set in advance
(`EvalBars`, recorded in `docs/M3_SHADOW.md`) rather than a constant somebody nudged
after seeing the result.

**Every result carries the provenance of the corpus it was computed from.** §13 risk 5
is that the labelling gets skipped, and its symptom is not an error — it is a plausible
number computed against placeholder data. `EvalResult.provenance` and `measured` make
"we scored 84%" and "we scored 84% on a corpus nobody labelled" different strings.

**Eval 8 has no bar, deliberately**, and this module refuses to invent one. §11: *"You
cannot know what injected memory costs per call until you measure it in your own system,
on your own prompts, with your own model. Measure it in shadow mode, then set a budget."*
It reports the number and the M2 comparison; the budget goes in `Settings` afterwards,
by hand, which is what makes it a decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from runtime.domain.enums import MemoryScope, MemoryStatus, MemoryTrust
from runtime.domain.ids import MemoryId, OrganizationId
from runtime.domain.memory import ScopeFilter, ScopeKey
from runtime.memory.golden import GoldenSet
from runtime.memory.golden import load as load_golden
from runtime.memory.store import MemoryStore
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.evals")


@dataclass(frozen=True, slots=True)
class EvalBars:
    """§11's bars. `[CHOSEN]` except where the plan marks them hard gates.

    *"Bars marked `[CHOSEN]` are starting positions, not findings. Set them before you
    measure."* They are a frozen value object for that reason: changing one is a diff.
    """

    fact_recall: float = 0.80
    retrieval_precision: float = 0.60
    irrelevant_rate: float = 0.05
    contradiction_handling: float = 1.00
    summary_fidelity: float = 0.90
    # 4, 5 and 9 have no field. Their bar is zero and zero is not a dial.


@dataclass(frozen=True, slots=True)
class EvalResult:
    number: int
    name: str
    value: float | None
    bar: float | None
    unit: str = "ratio"
    hard_gate: bool = False
    measured: bool = True
    provenance: str = "unknown"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passes(self) -> bool | None:
        """`None` when nothing was measured, which is not the same as failing.

        An eval with no corpus has not failed; it has not run. Collapsing the two would
        make an unlabelled golden set look like a quality problem, and the response to
        those two states is completely different."""
        if not self.measured or self.value is None or self.bar is None:
            return None
        # Only eval 3 (irrelevant rate) is a ceiling; everything else is a floor.
        return self.value <= self.bar if self.number == 3 else self.value >= self.bar

    def render(self) -> str:
        if not self.measured:
            return f"  {self.number}. {self.name:<24} not measured — {self.detail.get('why', '')}"
        verdict = {True: "pass", False: "FAIL", None: "—"}[self.passes]
        bar = f"bar {self.bar}" if self.bar is not None else "no bar (§11)"
        gate = "  [HARD GATE]" if self.hard_gate else ""
        value = "—" if self.value is None else f"{self.value:.3f}"
        return f"  {self.number}. {self.name:<24} {value:>8}   {bar:<16} {verdict}{gate}"


class EvalSuite:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: MemoryStore,
        *,
        bars: EvalBars | None = None,
        golden: GoldenSet | None = None,
    ) -> None:
        self._uow = uow_factory
        self._store = store
        self._bars = bars or EvalBars()
        self._golden = golden if golden is not None else load_golden()

    async def run_all(self, organization_id: OrganizationId) -> list[EvalResult]:
        return [
            await self.fact_recall(organization_id),
            await self.retrieval_precision(organization_id),
            await self.irrelevant_rate(organization_id),
            await self.cross_actor_leakage(organization_id),
            await self.cross_scope_leakage(organization_id),
            await self.contradiction_handling(organization_id),
            await self.summary_fidelity(organization_id),
            await self.token_effect(organization_id),
            await self.quarantine_containment(organization_id),
        ]

    # --- 1 ------------------------------------------------------------------------

    async def fact_recall(self, organization_id: OrganizationId) -> EvalResult:
        """*"golden facts.jsonl — retrievable at all?"*

        Retrievable **at all**, which is why this queries the store with the fact's own
        text and asks only whether the fact comes back — not whether it comes back
        first. Rank is eval 2's question. Separating them is what makes a bad number
        diagnosable: recall failing is a store or embedder problem, precision failing
        with recall fine is a rerank problem, and one combined number would leave you
        tuning the wrong one.
        """
        facts = [f for f in self._golden.facts if f.get("recurred")]
        if not facts:
            return _unmeasured(
                1,
                "fact recall",
                self._bars.fact_recall,
                self._golden.provenance,
                "facts.jsonl is empty or has no recurring facts (§10; build it from real runs)",
            )
        texts = await self._store.fetch([MemoryId(f["memory_id"]) for f in facts])
        found = 0
        for fact in facts:
            memory_id = MemoryId(fact["memory_id"])
            body = texts.get(memory_id)
            if body is None:
                continue
            hits = await self._store.search(
                organization_id=organization_id,
                query=body,
                scope_values=[f"{fact['scope']}:{fact['scope_id']}"],
                top_k=20,
            )
            found += any(h.memory_id == memory_id for h in hits)
        return EvalResult(
            1,
            "fact recall",
            found / len(facts),
            self._bars.fact_recall,
            provenance=self._golden.provenance,
            detail={"found": found, "of": len(facts)},
        )

    # --- 2 ------------------------------------------------------------------------

    async def retrieval_precision(self, organization_id: OrganizationId) -> EvalResult:
        """*"of injected, how many labelled correct?"*

        **Refuses to score unlabelled rows.** A `queries.jsonl` straight out of
        `GoldenBuilder` has `expected_memory_ids: []` on every row, and scoring those
        would give 0% (nothing expected was found) or 100% (nothing expected was
        missed) depending on which way the arithmetic fell — either way a number about
        the labelling rather than about retrieval. §13 risk 5 in one branch.
        """
        labelled = [q for q in self._golden.queries if q.get("expected_memory_ids")]
        if not labelled:
            total = len(self._golden.queries)
            return _unmeasured(
                2,
                "retrieval precision",
                self._bars.retrieval_precision,
                self._golden.provenance,
                f"{total} queries present, none hand-labelled (§10's day of work)",
            )
        correct = 0
        injected = 0
        for query in labelled:
            expected = set(query["expected_memory_ids"])
            hits = await self._store.search(
                organization_id=organization_id,
                query=query["query"],
                scope_values=list(query.get("scopes") or []),
                top_k=6,
            )
            injected += len(hits)
            correct += sum(1 for h in hits if h.memory_id in expected)
        return EvalResult(
            2,
            "retrieval precision",
            correct / injected if injected else 0.0,
            self._bars.retrieval_precision,
            provenance=self._golden.provenance,
            detail={"correct": correct, "injected": injected, "queries": len(labelled)},
        )

    # --- 3 ------------------------------------------------------------------------

    async def irrelevant_rate(self, organization_id: OrganizationId) -> EvalResult:
        """*"share graded neither helpful nor neutral"* — i.e. the harmful share.

        Read off the hand-graded traces, not computed. §13 risk 1 is that memory can
        make output *worse*, and that is a judgement about a specific call that only a
        person looking at that call can make.
        """
        async with self._uow() as uow:
            summary = await uow.traces.grade_summary(organization_id)
        graded = int(summary.get("graded") or 0)
        if not graded:
            return _unmeasured(
                3,
                "irrelevant rate",
                self._bars.irrelevant_rate,
                self._golden.provenance,
                "no traces graded yet (`runtime.cli memory grade`)",
            )
        return EvalResult(
            3,
            "irrelevant rate",
            (summary.get("harmful_pct") or 0.0) / 100.0,
            self._bars.irrelevant_rate,
            provenance="real",
            detail=summary,
        )

    # --- 4 and 5: the isolation gates ---------------------------------------------

    async def cross_actor_leakage(self, organization_id: OrganizationId) -> EvalResult:
        """T49. Actor A's private memories are never returned to actor B. **Zero.**

        Every private-scope memory in the organization is probed with its own text from
        every *other* actor's filter. Its own text, because that is the query most
        likely to retrieve it — a probe that used a paraphrase would pass by being a bad
        query rather than by isolation holding.
        """
        async with self._uow() as uow:
            actors = (
                (
                    await uow.session.execute(
                        text(
                            """
                        SELECT DISTINCT scope_id FROM memory_metadata
                         WHERE organization_id = :org AND scope = 'private'
                        """
                        ),
                        {"org": organization_id},
                    )
                )
                .scalars()
                .all()
            )
        escapes = await self._probe_scopes(
            organization_id,
            [ScopeKey(MemoryScope.PRIVATE_ACTOR, uuid.UUID(str(a))) for a in actors],
        )
        return EvalResult(
            4,
            "cross-actor leakage",
            float(len(escapes)),
            0.0,
            unit="escapes",
            hard_gate=True,
            provenance="real",
            detail={"escapes": escapes[:10], "actors": len(actors)},
        )

    async def cross_scope_leakage(self, organization_id: OrganizationId) -> EvalResult:
        """T48. **Zero.** Same probe, across every scope level rather than every actor."""
        async with self._uow() as uow:
            scopes = (
                await uow.session.execute(
                    text(
                        """
                        SELECT DISTINCT scope, scope_id FROM memory_metadata
                         WHERE organization_id = :org
                        """
                    ),
                    {"org": organization_id},
                )
            ).all()
        keys = [ScopeKey(MemoryScope(r.scope), uuid.UUID(str(r.scope_id))) for r in scopes]
        escapes = await self._probe_scopes(organization_id, keys, cross_level=True)
        return EvalResult(
            5,
            "cross-scope leakage",
            float(len(escapes)),
            0.0,
            unit="escapes",
            hard_gate=True,
            provenance="real",
            detail={"escapes": escapes[:10], "scopes": len(keys)},
        )

    async def _probe_scopes(
        self,
        organization_id: OrganizationId,
        keys: Sequence[ScopeKey],
        *,
        cross_level: bool = False,
    ) -> list[dict[str, str]]:
        """The shared machinery of evals 4 and 5, and of T48/T49.

        For each scope, take its memories and ask for them from every *other* scope's
        filter. An escape is any memory that `authorize` returns to a filter that does
        not name its scope.

        **Wider scopes are excluded from the cross-level probe.** A department memory
        being visible to a run in that department is the feature, not a leak; the
        probe compares only scopes where neither contains the other, which is what
        `readable_scopes` guarantees is impossible.
        """
        escapes: list[dict[str, str]] = []
        for owner in keys:
            async with self._uow() as uow:
                mine = await uow.memories.in_scope(
                    organization_id, scope=owner.scope, scope_id=owner.scope_id, limit=50
                )
            if not mine:
                continue
            texts = await self._store.fetch([m.memory_id for m in mine])
            for other in keys:
                if other == owner:
                    continue
                if cross_level and (
                    other.scope.wider_than(owner.scope) or owner.scope.wider_than(other.scope)
                ):
                    continue
                probe_filter = ScopeFilter(
                    organization_id=organization_id,
                    scopes=(other,),
                    statuses=frozenset({MemoryStatus.ACTIVE}),
                )
                async with self._uow() as uow:
                    permitted = await uow.memories.authorize(
                        probe_filter, [m.memory_id for m in mine]
                    )
                for memory_id in permitted:
                    escapes.append(
                        {
                            "memory_id": str(memory_id),
                            "owner_scope": str(owner),
                            "probe_scope": str(other),
                            "text": texts.get(memory_id, "")[:80],
                        }
                    )
        return escapes

    # --- 6 ------------------------------------------------------------------------

    async def contradiction_handling(self, organization_id: OrganizationId) -> EvalResult:
        """*"contradictions.jsonl — 100%"*: old superseded, only new retrieved.

        Both halves are checked. Marking the old one superseded but still returning it
        from a search is the failure that looks like success in the metadata and
        produces two contradictory facts in the prompt — which is eval 3's harmful
        category arriving by a route eval 6 was supposed to close.
        """
        pairs = self._golden.contradictions
        if not pairs:
            return _unmeasured(
                6,
                "contradiction handling",
                self._bars.contradiction_handling,
                self._golden.provenance,
                "contradictions.jsonl is empty",
            )
        handled = 0
        for pair in pairs:
            old_id = MemoryId(pair["superseded_memory_id"])
            new_id = MemoryId(pair["superseding_memory_id"])
            async with self._uow() as uow:
                old = await uow.memories.get(old_id)
                new = await uow.memories.get(new_id)
            if old is None or new is None or old.status is not MemoryStatus.SUPERSEDED:
                continue
            texts = await self._store.fetch([new_id])
            hits = await self._store.search(
                organization_id=organization_id,
                query=texts.get(new_id, ""),
                scope_values=[f"{pair['scope']}:{pair['scope_id']}"],
                top_k=10,
            )
            async with self._uow() as uow:
                permitted = await uow.memories.authorize(
                    ScopeFilter(
                        organization_id=organization_id,
                        scopes=(ScopeKey(MemoryScope(pair["scope"]), uuid.UUID(pair["scope_id"])),),
                    ),
                    [h.memory_id for h in hits],
                )
            handled += old_id not in permitted
        return EvalResult(
            6,
            "contradiction handling",
            handled / len(pairs),
            self._bars.contradiction_handling,
            provenance=self._golden.provenance,
            detail={"handled": handled, "of": len(pairs)},
        )

    # --- 7 ------------------------------------------------------------------------

    async def summary_fidelity(self, organization_id: OrganizationId) -> EvalResult:
        """*"key facts surviving summarization"*.

        Over `DERIVED` memories — the ones consolidation produced by compressing others
        — measured as the share whose superseded sources are still recoverable through
        the supersession chain. With consolidation shipping without a summarizing step
        (see `MemoryService.consolidate`, which says why), this reports "not measured"
        rather than a fabricated 100%: there is nothing summarized yet to lose anything.

        That is the honest state and it is deliberately left visible, because eval 7
        having a number is the precondition for turning summarizing consolidation on.
        """
        async with self._uow() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        """
                        SELECT count(*) FILTER (WHERE trust = 'DERIVED') AS derived,
                               count(*) FILTER (WHERE trust = 'DERIVED'
                                                AND supersedes IS NOT NULL) AS traceable
                          FROM memory_metadata WHERE organization_id = :org
                        """
                    ),
                    {"org": organization_id},
                )
            ).one()
        derived = int(rows.derived)
        if not derived:
            return _unmeasured(
                7,
                "summary fidelity",
                self._bars.summary_fidelity,
                "real",
                "no DERIVED memories: summarizing consolidation is not enabled yet",
            )
        return EvalResult(
            7,
            "summary fidelity",
            int(rows.traceable) / derived,
            self._bars.summary_fidelity,
            provenance="real",
            detail={"derived": derived, "traceable": int(rows.traceable)},
        )

    # --- 8 ------------------------------------------------------------------------

    async def token_effect(self, organization_id: OrganizationId) -> EvalResult:
        """*"injected tokens per call vs M2 baseline — measured, then budgeted"*.

        **No bar, and this method will not invent one.** It reports the mean tokens per
        retrieval — real when live, counterfactual when shadow — and leaves the budget
        to `Settings.memory_max_injected_tokens`, set by a person who has seen this
        number. §11: *"Eval 8 has no bar on purpose."*
        """
        async with self._uow() as uow:
            weeks = await uow.traces.token_effect(organization_id)
        if not weeks:
            return _unmeasured(8, "token effect", None, "real", "no retrievals traced yet")
        live = [w for w in weeks if not w["shadow_mode"]]
        rows = live or weeks
        key = "avg_injected_tokens" if live else "avg_would_have_tokens"
        total_retrievals = sum(int(w["retrievals"]) for w in rows)
        weighted = sum(float(w[key] or 0) * int(w["retrievals"]) for w in rows)
        return EvalResult(
            8,
            "token effect",
            weighted / total_retrievals if total_retrievals else 0.0,
            None,
            unit="tokens/call",
            provenance="real",
            detail={
                "mode": "live" if live else "shadow (counterfactual)",
                "retrievals": total_retrievals,
                "budget_setting": "RUNTIME_MEMORY_MAX_INJECTED_TOKENS",
                "weeks": rows[:4],
            },
        )

    # --- 9 ------------------------------------------------------------------------

    async def quarantine_containment(self, organization_id: OrganizationId) -> EvalResult:
        """T50 and T52. **Zero escapes.**

        Three ways a quarantined memory can escape, all counted here because they fail
        independently: it is `active` (the CHECK should have refused the row), it sits
        at a scope wider than private (nothing untrusted may be departmental), or
        `authorize` returns it to a filter belonging to a different actor.
        """
        escapes: list[dict[str, str]] = []
        async with self._uow() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        """
                        SELECT memory_id, status, scope, source_actor_id
                          FROM memory_metadata
                         WHERE organization_id = :org AND trust = 'UNTRUSTED_QUARANTINE'
                        """
                    ),
                    {"org": organization_id},
                )
            ).all()

        for row in rows:
            if row.status == MemoryStatus.ACTIVE.value:
                escapes.append({"memory_id": row.memory_id, "how": "quarantined row is active"})
            if row.scope != MemoryScope.PRIVATE_ACTOR.value:
                escapes.append(
                    {"memory_id": row.memory_id, "how": f"quarantined at scope {row.scope}"}
                )

        others = await self._other_actor_ids(organization_id, rows)
        for actor_id in others:
            probe = ScopeFilter(
                organization_id=organization_id,
                scopes=(ScopeKey(MemoryScope.PRIVATE_ACTOR, actor_id),),
                include_quarantined_for_actor=actor_id,
                statuses=frozenset({MemoryStatus.ACTIVE, MemoryStatus.QUARANTINED}),
            )
            async with self._uow() as uow:
                permitted = await uow.memories.authorize(
                    probe, [MemoryId(r.memory_id) for r in rows]
                )
            for memory_id, record in permitted.items():
                if record.trust is MemoryTrust.UNTRUSTED_QUARANTINE:
                    escapes.append(
                        {"memory_id": str(memory_id), "how": f"visible to actor {actor_id}"}
                    )
        return EvalResult(
            9,
            "quarantine containment",
            float(len(escapes)),
            0.0,
            unit="escapes",
            hard_gate=True,
            provenance="real",
            detail={"quarantined": len(rows), "escapes": escapes[:10]},
        )

    async def _other_actor_ids(
        self, organization_id: OrganizationId, rows: Sequence[Any]
    ) -> list[uuid.UUID]:
        owners = {str(r.source_actor_id) for r in rows if r.source_actor_id}
        async with self._uow() as uow:
            actors = (
                (
                    await uow.session.execute(
                        text("SELECT id FROM actors WHERE organization_id = :org"),
                        {"org": organization_id},
                    )
                )
                .scalars()
                .all()
            )
        return [uuid.UUID(str(a)) for a in actors if str(a) not in owners]


def _unmeasured(number: int, name: str, bar: float | None, provenance: str, why: str) -> EvalResult:
    return EvalResult(
        number, name, None, bar, measured=False, provenance=provenance, detail={"why": why}
    )


def render_all(results: Sequence[EvalResult]) -> str:
    lines = ["The nine memory evals (§11)", ""]
    lines += [r.render() for r in results]
    gates = [r for r in results if r.hard_gate]
    failed_gates = [r for r in gates if r.passes is False]
    unmeasured = [r for r in results if not r.measured]
    lines += [
        "",
        f"hard gates: {len(gates) - len(failed_gates)}/{len(gates)} at zero"
        + ("" if not failed_gates else f"  ← {', '.join(r.name for r in failed_gates)}"),
    ]
    if unmeasured:
        lines.append(
            f"not measured: {', '.join(r.name for r in unmeasured)}"
            "  (§13 risk 5 — an unmeasured eval is not a passing eval)"
        )
    provenances = {r.provenance for r in results if r.measured}
    if provenances - {"real"}:
        lines.append(
            f"corpus provenance: {', '.join(sorted(provenances))}"
            "  ← numbers over a placeholder corpus are not evidence (§10)"
        )
    return "\n".join(lines)
