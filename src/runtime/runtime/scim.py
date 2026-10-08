"""SCIM 2.0 provisioning — an identity provider keeping an organization's members in step.

An admin makes a token (`EnterpriseService.new_scim_token`) and gives the provider it
and the base URL. The provider then creates members when people are assigned the app,
updates their names and emails, and deactivates them when they leave or are unassigned
— which here suspends the member: their sessions end and their links stop working at
once, and their name stays on what they said (a member is never deleted). Reactivating
restores them. A provisioned member joins as a `member`; roles stay the admins' to give.

The organization's last active owner cannot be deactivated from here: that would lock
the organization out of its own admin settings with no way back but the CLI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from runtime.domain.members import MemberError, clean_email, token_hash
from runtime.org.audit import record
from runtime.persistence.repositories.members import MemberRow
from runtime.persistence.uow import UnitOfWorkFactory


class ScimConflictError(MemberError):
    pass


class ScimNotFoundError(MemberError):
    pass


@dataclass(frozen=True)
class Changes:
    email: str | None = None
    name: str | None = None
    active: bool | None = None
    external_id: str | None = None


class ScimService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def organization_for(self, token: str) -> uuid.UUID | None:
        if not token or len(token) > 200:
            return None
        async with self._uow.transaction() as uow:
            return await uow.scim.organization_for(token_hash(token))

    async def users(self, organization_id: uuid.UUID) -> list[MemberRow]:
        async with self._uow() as uow:
            return await uow.members.for_organization(organization_id)

    async def user(self, organization_id: uuid.UUID, member_id: uuid.UUID) -> MemberRow:
        async with self._uow() as uow:
            row = await uow.members.get(member_id)
        if row is None or row.organization_id != organization_id:
            raise ScimNotFoundError(f"no user {member_id}")
        return row

    async def create(
        self,
        organization_id: uuid.UUID,
        *,
        email: str,
        name: str,
        active: bool,
        external_id: str | None,
    ) -> MemberRow:
        email = clean_email(email)
        member_id = uuid.uuid4()
        async with self._uow.transaction() as uow:
            if not await uow.members.add(
                member_id,
                organization_id,
                email=email,
                name=name[:80],
                role="member",
                external_id=external_id,
            ):
                raise ScimConflictError(f"{email} is already a member")
            if not active:
                await uow.members.update(member_id, {"status": "suspended"})
            await record(
                uow, organization_id, "scim.user_created", actor_label="scim", target=email
            )
            row = await uow.members.get(member_id)
        assert row is not None
        return row

    async def change(
        self, organization_id: uuid.UUID, member_id: uuid.UUID, changes: Changes
    ) -> MemberRow:
        async with self._uow.transaction() as uow:
            row = await uow.members.get(member_id)
            if row is None or row.organization_id != organization_id:
                raise ScimNotFoundError(f"no user {member_id}")
            fields: dict[str, object] = {}
            if changes.email is not None:
                email = clean_email(changes.email)
                other = await uow.members.by_email(organization_id, email)
                if other is not None and other.id != member_id:
                    raise ScimConflictError(f"{email} is already a member")
                fields["email"] = email
            if changes.name is not None:
                fields["name"] = changes.name[:80]
            if changes.external_id is not None:
                fields["external_id"] = changes.external_id
            action = "scim.user_updated"
            if changes.active is not None and changes.active != (row.status == "active"):
                if not changes.active:
                    if row.role == "owner" and await uow.members.owners(organization_id) <= 1:
                        raise ScimConflictError(
                            "this is the organization's last owner; make someone else an "
                            "owner first"
                        )
                    fields["status"] = "suspended"
                    await uow.members.drop_sessions(member_id)
                    await uow.members.revoke_links_for(organization_id, row.email)
                    action = "scim.user_deactivated"
                else:
                    fields["status"] = "active"
                    action = "scim.user_reactivated"
            await uow.members.update(member_id, fields)
            if fields:
                await record(
                    uow,
                    organization_id,
                    action,
                    actor_label="scim",
                    target=str(fields.get("email", row.email)),
                    detail={"changed": sorted(fields)},
                )
            changed = await uow.members.get(member_id)
        assert changed is not None
        return changed
