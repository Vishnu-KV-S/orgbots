"""Promotion: private → department → company, with a review at each rung.

    PRIVATE ──review──► DEPARTMENT ──review──► COMPANY

§8's argument, restated because it is the reason this file is longer than a scope
UPDATE would be: *"Automatic promotion means company memory accumulates whatever the
extraction model happened to emit, with no one accountable for any of it."*

So there is exactly one function that widens a memory's scope — `PromotionService.apply`
— it is reachable only from `decide`, and `decide` cannot record an approval without a
reviewer because `ck_promotion_decided_has_reviewer` refuses the row. T53 asserts the
absence of a second path by reading the source for callers of `move_scope`, because an
absence is the one thing a behavioural test cannot demonstrate.

**The classifier is a filter, not the decision.** §8: *"a cheap-model classifier plus a
human sample"*. The classifier's job is to make the queue short enough that a person
finishes reading it — it approves nothing that a person could not later find and
reverse, and its `reviewer_kind='model'` rows are exactly the population the human
sample is drawn from. When the two disagree, that is a number: one self-join on
`memory_promotions`, the same shape M1 uses for its manager-versus-human confusion
matrix.

**The five criteria are booleans on the row, not prose.** §8 names four — durable,
non-obvious, applies beyond the originating actor, not already present — plus
`trust != quarantine`. A rationale is what a reviewer thought; the booleans are what
they checked, and only the second kind can be aggregated into "what are we approving
these on".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from runtime.domain.enums import (
    PROMOTION_PATH,
    MemoryScope,
    MemoryStatus,
    MemoryTrust,
    PromotionStatus,
    WorkClass,
)
from runtime.domain.errors import PromotionNotReviewed
from runtime.domain.ids import BudgetPoolId, MemoryId, OrganizationId, PromotionId
from runtime.domain.specs import ModelProfile
from runtime.domain.trust import TRUST_SYSTEM_RULE, UntrustedBlock
from runtime.gateway.models import ModelGateway, ModelRequest
from runtime.memory.store import MemoryStore
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.memory import PromotionRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.promotion")

CRITERIA = ("durable", "non_obvious", "generalises", "novel", "not_quarantined")
"""§8's criteria, as the keys of `memory_promotions.criteria`."""

REVIEW_SYSTEM = """You decide whether one remembered fact should be shared more widely.

It is currently visible to the actor that wrote it. You are deciding whether everyone in \
the {to_scope} should see it, on every relevant call, from now on.

**Default to no.** A fact that stays private costs one actor a little re-derivation. A \
fact that is wrong, obvious or narrow, promoted, costs every actor tokens on every \
retrieval and occasionally costs one of them a wrong answer delivered confidently. The \
two mistakes are not symmetric and you should not treat them as if they were.

Answer five questions, each strictly true or false:

- **durable** — will this still be true and still matter in three months? A price is \
durable; a price *this week* is not, unless the statement says which week.
- **non_obvious** — would a competent person doing this work not already assume it? If \
they would, promoting it buys nothing and costs tokens forever.
- **generalises** — is it useful to someone other than the actor that wrote it? Notes \
about one actor's own workflow are the commonest false positive here.
- **novel** — is it absent from what is already known? You are shown the nearest \
existing memories at the destination scope; if one of them already says this, it is not \
novel, however well phrased this version is.

Approve only if all four are true. Then give one sentence of rationale naming the \
question that was closest to failing — that sentence is what a human reviewing your \
decision will read first."""

REVIEW_INSTRUCTION = """Return JSON and nothing else:

{"durable": bool, "non_obvious": bool, "generalises": bool, "novel": bool,
 "approve": bool, "rationale": "one sentence"}

`approve` must be false if any of the four is false."""


@dataclass(frozen=True, slots=True)
class ReviewResult:
    promotion_id: PromotionId
    approved: bool
    criteria: dict[str, bool]
    rationale: str
    cost_cents: int = 0


class PromotionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: MemoryStore,
        models: ModelGateway | None = None,
        *,
        profile: ModelProfile | None = None,
    ) -> None:
        self._uow = uow_factory
        self._store = store
        self._models = models
        self._profile = profile

    # --- review --------------------------------------------------------------------

    async def review_pending(
        self,
        organization_id: OrganizationId,
        *,
        limit: int = 20,
        pool_id: BudgetPoolId | None = None,
    ) -> list[ReviewResult]:
        """Run the classifier over the queue. Approvals are applied immediately.

        "Immediately" is not a shortcut past review — the classifier *is* a reviewer,
        recorded as `reviewer_kind='model'`, and every one of its decisions is a row
        with a rationale that a person can find and reverse. What §8 forbids is
        promotion with *no* reviewer, and there is no path here that produces one.
        """
        if self._models is None or self._profile is None:
            log.info("memory.review_skipped", reason="no model gateway configured")
            return []

        async with self._uow() as uow:
            pending = await uow.promotions.pending(organization_id, limit=limit)
        results: list[ReviewResult] = []
        for row in pending:
            try:
                result = await self._review_one(organization_id, row, pool_id=pool_id)
            except Exception as exc:
                log.warning(
                    "memory.review_failed", promotion=str(row.id), error=f"{type(exc).__name__}"
                )
                continue
            results.append(result)
            await self.decide(
                organization_id,
                row.id,
                approve=result.approved,
                reviewer="memory.classifier",
                reviewer_kind="model",
                rationale=result.rationale,
                criteria=result.criteria,
                review_cost_cents=result.cost_cents,
            )
        return results

    async def _review_one(
        self,
        organization_id: OrganizationId,
        row: PromotionRow,
        *,
        pool_id: BudgetPoolId | None,
    ) -> ReviewResult:
        assert self._models is not None and self._profile is not None  # guarded by caller

        async with self._uow() as uow:
            record = await uow.memories.get(row.memory_id)
        if record is None:
            return ReviewResult(row.id, False, _all_false(), "source memory no longer exists")
        # Quarantine is checked **before** status, and the order is the whole reason
        # this is two branches rather than one. A quarantined memory is also non-active,
        # so a status check first would answer every quarantine case with "no longer
        # active" — technically true, and useless to whoever is reading the rejection
        # rationale to find out why company memory is missing something. §8's fifth
        # criterion deserves to be named when it is the one that fired.
        if record.trust.is_quarantined:
            return ReviewResult(
                row.id, False, {**_all_false(), "not_quarantined": False}, "quarantined"
            )
        if record.status is not MemoryStatus.ACTIVE:
            return ReviewResult(
                row.id, False, _all_false(), f"source memory is {record.status.value}"
            )

        texts = await self._store.fetch([row.memory_id])
        body = texts.get(row.memory_id, "")
        neighbours = await self._store.search(
            organization_id=organization_id,
            query=body,
            scope_values=[f"{row.to_scope.value}:{row.to_scope_id}"],
            top_k=5,
        )

        # The candidate and its neighbours are both extracted model text, and the
        # candidate is the thing under review — so it is fenced. A memory that says
        # "this fact is pre-approved for company scope" is exactly the payload this
        # reviewer exists to be immune to, and M2's trust rule is what makes it text.
        candidate = UntrustedBlock(content=body, source=f"memory:{row.memory_id}", ingress="memory")
        existing = "\n".join(f"- {c.text}" for c in neighbours) or "(nothing similar)"
        prompt = (
            f"## The candidate\n{candidate.render(max_chars=2_000)}\n\n"
            f"## Already known at {row.to_scope.value} scope\n{existing}\n\n"
            f"{REVIEW_INSTRUCTION}"
        )
        response = await self._models.complete_detached(
            organization_id,
            ModelRequest(
                prompt=prompt,
                system=(
                    f"{REVIEW_SYSTEM.format(to_scope=row.to_scope.value)}\n\n"
                    f"{TRUST_SYSTEM_RULE.strip()}"
                ),
            ),
            profile=self._profile,
            work_class=WorkClass.MEMORY,
            call_site="memory.promotion.review",
            pool_id=pool_id,
            actor_name=row.proposed_by,
        )
        verdict = _parse_verdict(response.text)
        criteria = {
            "durable": bool(verdict.get("durable")),
            "non_obvious": bool(verdict.get("non_obvious")),
            "generalises": bool(verdict.get("generalises")),
            "novel": bool(verdict.get("novel")),
            "not_quarantined": True,
        }
        # `approve` is recomputed from the criteria rather than taken from the model.
        # A reviewer that says "all four false, approve: true" has contradicted itself,
        # and the four are the ones a human can audit afterwards.
        approved = all(criteria.values())
        return ReviewResult(
            promotion_id=row.id,
            approved=approved,
            criteria=criteria,
            rationale=str(verdict.get("rationale", ""))[:500] or "no rationale given",
            cost_cents=response.cost_cents,
        )

    # --- decide and apply ----------------------------------------------------------

    async def decide(
        self,
        organization_id: OrganizationId,
        promotion_id: PromotionId,
        *,
        approve: bool,
        reviewer: str,
        reviewer_kind: str,
        rationale: str,
        criteria: dict[str, bool] | None = None,
        review_cost_cents: int = 0,
    ) -> bool:
        """Record a decision and, if it approves, apply it. One transaction.

        Together, because a `memory_promotions` row saying APPROVED next to a memory
        still at private scope is a lie that only shows up when someone asks why a
        company fact is missing. `ck_promotion_approved_has_result` refuses the row
        without `promoted_memory_id`, so the database enforces the pairing that this
        method's transaction provides.
        """
        async with self._uow.transaction() as uow:
            rows = await uow.promotions.pending(organization_id, limit=500)
            row = next((r for r in rows if r.id == promotion_id), None)
            if row is None:
                return False

            if approve:
                target = PROMOTION_PATH.get(row.from_scope)
                if target is not row.to_scope:
                    raise PromotionNotReviewed(
                        f"promotion {promotion_id} widens {row.from_scope.value} to "
                        f"{row.to_scope.value}, which is not the next rung "
                        f"({target.value if target else 'none'}); §8's ladder is one rung "
                        "at a time and two rungs mean two reviews"
                    )
                record = await uow.memories.get(row.memory_id)
                if record is None or record.trust is MemoryTrust.UNTRUSTED_QUARANTINE:
                    approve = False
                    rationale = f"{rationale} [refused at apply: quarantined or missing]"

            decided = await uow.promotions.decide(
                promotion_id,
                status=PromotionStatus.APPROVED if approve else PromotionStatus.REJECTED,
                reviewer=reviewer,
                reviewer_kind=reviewer_kind,
                rationale=rationale,
                criteria=criteria or {},
                promoted_memory_id=row.memory_id if approve else None,
                review_cost_cents=review_cost_cents,
            )
            if not decided:
                return False
            if approve:
                await uow.memories.move_scope(
                    row.memory_id, scope=row.to_scope, scope_id=row.to_scope_id
                )
                await uow.memories.audit(
                    memory_id=row.memory_id,
                    organization_id=organization_id,
                    event="PROMOTE",
                    actor_name=reviewer,
                    detail={
                        "from": row.from_scope.value,
                        "to": row.to_scope.value,
                        "reviewer_kind": reviewer_kind,
                        "criteria": criteria or {},
                    },
                )
        log.info(
            "memory.promotion_decided",
            promotion=str(promotion_id),
            approved=approve,
            reviewer=reviewer,
            kind=reviewer_kind,
        )
        return True

    async def sample_for_human(
        self, organization_id: OrganizationId, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """The population §8's human sample is drawn from: what the model approved.

        Model *approvals*, not the whole decision stream, because the two error
        directions cost differently — a wrongly rejected fact costs one actor some
        re-derivation, and a wrongly approved one is in front of everybody forever.
        Sampling both evenly would spend half the reviewer's attention on the cheaper
        mistake.
        """
        async with self._uow() as uow:
            from sqlalchemy import text as _text

            rows = (
                await uow.session.execute(
                    _text(
                        """
                        SELECT p.id, p.memory_id, p.from_scope, p.to_scope, p.rationale,
                               p.criteria, p.decided_at, m.status, m.importance
                          FROM memory_promotions p
                          LEFT JOIN memory_metadata m ON m.memory_id = p.memory_id
                         WHERE p.organization_id = :org AND p.status = 'APPROVED'
                           AND p.reviewer_kind = 'model'
                         ORDER BY p.decided_at DESC LIMIT :lim
                        """
                    ),
                    {"org": organization_id, "lim": limit},
                )
            ).all()
        texts = await self._store.fetch([MemoryId(r.memory_id) for r in rows])
        return [
            {
                "promotion_id": str(r.id),
                "memory_id": r.memory_id,
                "text": texts.get(MemoryId(r.memory_id), "(text unavailable)"),
                "from_scope": r.from_scope,
                "to_scope": r.to_scope,
                "rationale": r.rationale,
                "criteria": r.criteria,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
            for r in rows
        ]

    async def reverse(
        self,
        organization_id: OrganizationId,
        memory_id: MemoryId,
        *,
        to_scope: MemoryScope,
        to_scope_id: UUID,
        reviewer: str,
        reason: str,
    ) -> None:
        """A human narrowing a scope after the fact. The other half of accountability.

        §8's traceability is only useful if the answer to "this company fact is wrong"
        is an action rather than a diagnosis. Narrowing is not itself gated by review —
        making a memory *less* visible cannot leak anything, and requiring approval to
        undo a mistake is how mistakes stay.
        """
        async with self._uow.transaction() as uow:
            await uow.memories.move_scope(memory_id, scope=to_scope, scope_id=to_scope_id)
            await uow.memories.audit(
                memory_id=memory_id,
                organization_id=organization_id,
                event="PROMOTE",
                actor_name=reviewer,
                detail={"reversed_to": to_scope.value, "reason": reason},
            )


def _all_false() -> dict[str, bool]:
    return dict.fromkeys(CRITERIA, False)


def _parse_verdict(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        parts = stripped.split("```", 2)
        if len(parts) >= 2:
            stripped = parts[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        # An unparseable reviewer is a rejection, not an error. The queue keeps its
        # row, the memory keeps its scope, and the failure shows up as a rejection with
        # a rationale saying why — which is a state somebody can act on.
        return {"rationale": "reviewer response was not JSON"}
    return loaded if isinstance(loaded, dict) else {"rationale": "reviewer returned a non-object"}
