"""The control plane's audit trail — one line per decision an admin or the runtime
made about an organization (migration 053, `org_audit_events`).

What is recorded is who, what and to what — never a secret, a token or a message body.
Recorded in the transaction of the change it describes where there is one, so a change
and its line commit or roll back together. Exported to the organization's collector by
`runtime.runtime.telemetry`.

Actions are dotted, area first: `member.invited`, `member.role_changed`, `sso.saved`,
`policy.changed`, `secret.set`, `bot.shared`, `template_link.made`, `scim.user_created`…
"""

from __future__ import annotations

import uuid
from typing import Any

from runtime.domain.members import Member
from runtime.persistence.uow import UnitOfWork


async def record(
    uow: UnitOfWork,
    organization_id: uuid.UUID,
    action: str,
    *,
    actor: Member | None = None,
    actor_label: str = "",
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    await uow.org_audit.record(
        organization_id,
        action,
        actor_member_id=actor.id if actor else None,
        actor=actor.email if actor else actor_label,
        target=target,
        detail=detail,
    )
