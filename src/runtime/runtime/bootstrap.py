"""Registering organizations and actors.

M0 has no YAML and no control plane, so actors are registered from code. The
important part is not the API but the invariant it preserves: an actor version is
immutable once written, and publishing a new version means inserting a row and
flipping a pointer, never editing a spec in place. In-flight runs hold a reference
to the version they were admitted under.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import ActorId, OrganizationId, new_actor_id
from runtime.domain.specs import ActorSpec
from runtime.persistence.uow import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class RegisteredActor:
    actor_id: ActorId
    version: int
    version_id: int
    spec_hash: str


class Registrar:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def ensure_organization(self, organization_id: OrganizationId, name: str) -> None:
        from sqlalchemy import text

        async with self._uow.transaction() as uow:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO organizations (id, name) VALUES (:id, :name)
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {"id": organization_id, "name": name},
            )

    async def publish_actor(
        self,
        organization_id: OrganizationId,
        spec: ActorSpec,
        *,
        actor_id: ActorId | None = None,
    ) -> RegisteredActor:
        """Create the actor if needed, append a new immutable version, activate it.

        Republishing an identical spec still creates a version. Versions are cheap
        and the alternative — silently reusing the previous version when the spec
        happens to match — makes "which version was this run admitted under"
        ambiguous at exactly the moment someone is trying to answer it.
        """
        from sqlalchemy import text

        payload = spec.model_dump(mode="json")
        spec_hash = canonical_hash(spec)

        async with self._uow.transaction() as uow:
            existing = (
                await uow.session.execute(
                    text("SELECT id FROM actors WHERE organization_id = :org AND name = :name"),
                    {"org": organization_id, "name": spec.name},
                )
            ).scalar_one_or_none()
            if existing is None:
                resolved_id = actor_id or new_actor_id()
                await uow.actors.create_actor(
                    resolved_id, organization_id, spec.name, spec.kind.value
                )
            else:
                resolved_id = ActorId(existing)

            next_version = int(
                (
                    await uow.session.execute(
                        text(
                            "SELECT COALESCE(max(version), 0) + 1 FROM actor_versions "
                            "WHERE actor_id = :id"
                        ),
                        {"id": resolved_id},
                    )
                ).scalar_one()
            )
            version_id = await uow.actors.add_version(resolved_id, next_version, payload, spec_hash)
            await uow.actors.set_active_version(resolved_id, version_id)

        return RegisteredActor(
            actor_id=resolved_id,
            version=next_version,
            version_id=version_id,
            spec_hash=spec_hash,
        )
