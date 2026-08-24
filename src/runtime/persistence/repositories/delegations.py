"""The `delegations` table — one row per delegation attempt, including the refused ones.

The row is written *before* the child exists and updated afterwards, which is the
ordering that makes replay safe: `record_attempt` is `ON CONFLICT DO NOTHING` on
`(parent_run_id, idempotency_key)`, so a replayed node either wins the key and spawns,
or loses it and is told which child it already spawned. There is no window in which a
node has spawned a child and not recorded it, because the record is what authorises the
spawn.

Compare `RunRepository.insert_if_absent`, which solves the same problem one level up
and for the same reason: a pre-flight SELECT passes for every concurrent caller and
then all but one collide on the index.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import DelegationStatus
from runtime.domain.ids import OrganizationId, RunId
from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class DelegationRow:
    id: uuid.UUID
    parent_run_id: RunId
    child_run_id: RunId | None
    root_run_id: RunId
    target_actor: str
    node: str
    ordinal: int
    idempotency_key: str
    status: DelegationStatus
    refusal_reason: str | None
    result: dict[str, Any] | None
    late: bool
    created_at: dt.datetime
    attached_at: dt.datetime | None

    @property
    def is_attached(self) -> bool:
        return self.result is not None


def _row(r: Any) -> DelegationRow:
    return DelegationRow(
        id=r.id,
        parent_run_id=RunId(r.parent_run_id),
        child_run_id=RunId(r.child_run_id) if r.child_run_id else None,
        root_run_id=RunId(r.root_run_id),
        target_actor=r.target_actor,
        node=r.node,
        ordinal=int(r.ordinal),
        idempotency_key=r.idempotency_key,
        status=DelegationStatus(r.status),
        refusal_reason=r.refusal_reason,
        result=r.result,
        late=bool(r.late),
        created_at=r.created_at,
        attached_at=r.attached_at,
    )


_COLUMNS = (
    "id, parent_run_id, child_run_id, root_run_id, target_actor, node, ordinal, "
    "idempotency_key, status, refusal_reason, result, late, created_at, attached_at"
)


class DelegationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def claim_key(
        self,
        row_id: uuid.UUID,
        *,
        organization_id: OrganizationId,
        parent_run_id: RunId,
        root_run_id: RunId,
        target_actor: str,
        node: str,
        ordinal: int,
        idempotency_key: str,
        child_run_id: RunId,
    ) -> DelegationRow | None:
        """Claim the key for a spawn. `None` means somebody already has it.

        Written in the same transaction as the child's `start_run()`, so "the child
        exists" and "the delegation is recorded" are one fact. T67 is the test and the
        failure it prevents is a subtree that costs twice what the ledger predicted.
        """
        inserted = (
            await self._s.execute(
                text(
                    f"""
                    INSERT INTO delegations (
                        id, organization_id, root_run_id, parent_run_id, child_run_id,
                        target_actor, node, ordinal, idempotency_key, status
                    ) VALUES (
                        :id, :org, :root, :parent, :child,
                        :target, :node, :ordinal, :key, :status
                    )
                    ON CONFLICT ON CONSTRAINT uq_delegation_key DO NOTHING
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": row_id,
                    "org": organization_id,
                    "root": root_run_id,
                    "parent": parent_run_id,
                    "child": child_run_id,
                    "target": target_actor,
                    "node": node,
                    "ordinal": ordinal,
                    "key": idempotency_key,
                    "status": DelegationStatus.SPAWNED.value,
                },
            )
        ).one_or_none()
        return _row(inserted) if inserted is not None else None

    async def record_refusal(
        self,
        row_id: uuid.UUID,
        *,
        organization_id: OrganizationId,
        parent_run_id: RunId,
        root_run_id: RunId,
        target_actor: str,
        node: str,
        ordinal: int,
        idempotency_key: str,
        reason: str,
    ) -> None:
        """A delegation refused at checks 1-6, which create no run.

        `DO NOTHING` on conflict rather than an update: if the key is already taken by
        a successful spawn, the refusal is this attempt's problem and not a reason to
        rewrite history. A replay that is refused where the original succeeded is
        itself worth seeing, and it shows up as a child with no matching refusal row.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO delegations (
                    id, organization_id, root_run_id, parent_run_id, child_run_id,
                    target_actor, node, ordinal, idempotency_key, status, refusal_reason
                ) VALUES (
                    :id, :org, :root, :parent, NULL,
                    :target, :node, :ordinal, :key, 'REFUSED', :reason
                )
                ON CONFLICT ON CONSTRAINT uq_delegation_key DO NOTHING
                """
            ),
            {
                "id": row_id,
                "org": organization_id,
                "root": root_run_id,
                "parent": parent_run_id,
                "target": target_actor,
                "node": node,
                "ordinal": ordinal,
                "key": idempotency_key,
                "reason": reason[:500],
            },
        )

    async def get_by_key(self, parent_run_id: RunId, idempotency_key: str) -> DelegationRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM delegations "
                    "WHERE parent_run_id = :parent AND idempotency_key = :key"
                ),
                {"parent": parent_run_id, "key": idempotency_key},
            )
        ).one_or_none()
        return _row(row) if row is not None else None

    async def get_by_child(self, child_run_id: RunId) -> DelegationRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM delegations WHERE child_run_id = :child"),
                {"child": child_run_id},
            )
        ).one_or_none()
        return _row(row) if row is not None else None

    async def for_parent(self, parent_run_id: RunId) -> list[DelegationRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM delegations WHERE parent_run_id = :parent "
                    "ORDER BY node, ordinal, created_at"
                ),
                {"parent": parent_run_id},
            )
        ).all()
        return [_row(r) for r in rows]

    async def attach_result(
        self,
        child_run_id: RunId,
        *,
        result: dict[str, Any],
        status: DelegationStatus,
        late: bool,
    ) -> bool:
        """Persist a child's result against its delegation row. Idempotent.

        The `attached_at IS NULL` predicate is the idempotency: a redelivered
        `run.succeeded` event attaches once. Without it, a late attach would overwrite
        a result the parent had already read, and the parent would have summarised one
        answer while the record showed another.

        `late` is stored rather than derived from timestamps. Deriving it would mean
        comparing the child's `ended_at` to the parent's, which are written by two
        different transactions on two different workers — a comparison that is right
        most of the time and unfalsifiable when it is not.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE delegations
                       SET result = CAST(:result AS jsonb),
                           status = :status,
                           late = :late,
                           attached_at = now()
                     WHERE child_run_id = :child AND attached_at IS NULL
                    RETURNING id
                    """
                ),
                {
                    "child": child_run_id,
                    "result": to_jsonb(result),
                    "status": status.value,
                    "late": late,
                },
            )
        ).one_or_none()
        return row is not None

    async def mark_cancelled(self, child_run_ids: list[RunId]) -> int:
        """Follow a cascade cancel into the delegation rows.

        Only rows still `SPAWNED` move. A child that completed before the cascade
        reached it keeps `COMPLETED` — the work happened, and the row is the evidence
        that the tree paid for it whether or not anybody read the answer.
        """
        if not child_run_ids:
            return 0
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE delegations SET status = 'CANCELLED'
                     WHERE child_run_id = ANY(:children) AND status = 'SPAWNED'
                    RETURNING id
                    """
                ),
                {"children": list(child_run_ids)},
            )
        ).all()
        return len(rows)
