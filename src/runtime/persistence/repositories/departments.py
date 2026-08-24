"""Departments — the table M4 deferred and M5 needs.

The two derived scope ids are *arguments* to `ensure`, not computations inside it.
`runtime.budget.service.department_scope_id` and `runtime.org.department.department_scope_id`
are the definitions; this layer stores what they produced. A repository that derived
them itself would be a third definition, and the first time one of the three drifted
the symptom would be a department whose memories and whose budget pool belonged to two
different departments — which is a cross-scope read and a mis-billed pool, reported as
neither.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.ids import OrganizationId


@dataclass(frozen=True, slots=True)
class DepartmentRow:
    id: uuid.UUID
    """Also the department's `budget_pools.scope_id`. See migration 034."""
    organization_id: OrganizationId
    name: str
    parent_id: uuid.UUID | None
    head_actor_name: str | None
    description: str
    memory_scope_id: uuid.UUID
    active: bool


class DepartmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def ensure(
        self,
        department_id: uuid.UUID,
        organization_id: OrganizationId,
        name: str,
        *,
        memory_scope_id: uuid.UUID,
        parent_id: uuid.UUID | None = None,
        head_actor_name: str | None = None,
        description: str = "",
    ) -> uuid.UUID:
        """Upsert one department and return its id.

        `parent_id` and `head_actor_name` are updated on conflict but the two scope
        ids never are — they are the primary key and a unique column, and a department
        whose scope id moved would orphan every memory and every budget pool written
        under the old one. That is the failure `031`'s comment about renames is really
        about, and here it is structurally impossible rather than merely discouraged.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO departments (id, organization_id, name, parent_id,
                                         head_actor_name, description, memory_scope_id)
                VALUES (:id, :org, :name, :parent, :head, :description, :mem)
                ON CONFLICT ON CONSTRAINT uq_department_name DO UPDATE
                   SET parent_id = EXCLUDED.parent_id,
                       head_actor_name = EXCLUDED.head_actor_name,
                       description = EXCLUDED.description,
                       active = true
                """
            ),
            {
                "id": department_id,
                "org": organization_id,
                "name": name,
                "parent": parent_id,
                "head": head_actor_name,
                "description": description,
                "mem": memory_scope_id,
            },
        )
        return department_id

    async def get(self, organization_id: OrganizationId, name: str) -> DepartmentRow | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, name, parent_id, head_actor_name,
                           description, memory_scope_id, active
                      FROM departments WHERE organization_id = :org AND name = :name
                    """
                ),
                {"org": organization_id, "name": name},
            )
        ).one_or_none()
        if row is None:
            return None
        return DepartmentRow(
            id=row.id,
            organization_id=OrganizationId(row.organization_id),
            name=row.name,
            parent_id=row.parent_id,
            head_actor_name=row.head_actor_name,
            description=row.description,
            memory_scope_id=row.memory_scope_id,
            active=bool(row.active),
        )

    async def all_for(self, organization_id: OrganizationId) -> list[DepartmentRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, name, parent_id, head_actor_name,
                           description, memory_scope_id, active
                      FROM departments WHERE organization_id = :org ORDER BY name
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            DepartmentRow(
                id=r.id,
                organization_id=OrganizationId(r.organization_id),
                name=r.name,
                parent_id=r.parent_id,
                head_actor_name=r.head_actor_name,
                description=r.description,
                memory_scope_id=r.memory_scope_id,
                active=bool(r.active),
            )
            for r in rows
        ]

    async def deactivate_missing(
        self, organization_id: OrganizationId, keep: list[str]
    ) -> list[str]:
        """Edge case 78's rule for departments: removed from the YAML is deactivated.

        Never deleted. A department row is referenced by memories written under its
        scope and by budget pools charged against it, and a hard delete would make a
        historical spend report unable to name what it was spent on.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE departments SET active = false
                     WHERE organization_id = :org AND active AND NOT (name = ANY(:keep))
                    RETURNING name
                    """
                ),
                {"org": organization_id, "keep": list(keep)},
            )
        ).all()
        return [r.name for r in rows]
