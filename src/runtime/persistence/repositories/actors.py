"""Actors and their immutable versions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.errors import UnknownActorError
from runtime.domain.ids import ActorId
from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class ResolvedActor:
    actor_id: ActorId
    actor_version_id: int
    version: int
    kind: str
    spec: dict[str, Any]
    spec_hash: str
    delegation: dict[str, Any] | None = None
    """M5. The actor's `DelegationDoc`, as applied.

    On the actor *row* rather than in `spec`, and therefore outside `spec_hash`. M4
    kept `DelegationDoc` out of `ActorSpec` because adding a field there changes every
    hash in the department and fails §8's round-trip; M5 does not undo that decision to
    read a number it can read from a column. Frozen into `RunSpec.delegation` at
    admission, which is where it becomes immutable for the life of the run.

    `None` for every actor that has no delegation block, which is every actor today.
    """


class ActorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def active_names(self, organization_id: uuid.UUID) -> list[str]:
        """Who this organization's actors currently are.

        The dispatcher's answer to "is this recipient an actor". M1 answered it from a
        frozenset in Python, which stopped being true the moment an organization could
        be defined in a file.
        """
        rows = (
            await self._s.execute(
                text(
                    "SELECT name FROM actors WHERE organization_id = :org AND active ORDER BY name"
                ),
                {"org": organization_id},
            )
        ).all()
        return [r.name for r in rows]

    async def departments_by_name(self, organization_id: uuid.UUID) -> dict[str, str]:
        """actor name → department name, for the actors that have one.

        One query rather than a join onto every kill-switch read: the caller wants this
        only while a department-scoped switch is engaged. See
        `runtime.org.killswitch.KillSwitchService.departments_by_actor`.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT name, department FROM actors
                     WHERE organization_id = :org AND active AND department IS NOT NULL
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.name: r.department for r in rows}

    async def create_actor(
        self, actor_id: ActorId, organization_id: uuid.UUID, name: str, kind: str
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO actors (id, organization_id, name, kind)
                VALUES (:id, :org, :name, :kind)
                """
            ),
            {"id": actor_id, "org": organization_id, "name": name, "kind": kind},
        )

    async def add_version(
        self, actor_id: ActorId, version: int, spec: dict[str, Any], spec_hash: str
    ) -> int:
        version_id = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO actor_versions (actor_id, version, spec, spec_hash)
                    VALUES (:actor_id, :version, CAST(:spec AS jsonb), :spec_hash)
                    RETURNING id
                    """
                ),
                {
                    "actor_id": actor_id,
                    "version": version,
                    "spec": to_jsonb(spec),
                    "spec_hash": spec_hash,
                },
            )
        ).scalar_one()
        return int(version_id)

    async def set_active_version(self, actor_id: ActorId, version_id: int) -> None:
        await self._s.execute(
            text("UPDATE actors SET active_version_id = :v WHERE id = :id"),
            {"v": version_id, "id": actor_id},
        )

    async def resolve_active(self, organization_id: uuid.UUID, name: str) -> ResolvedActor:
        """Resolve `name` to the currently active version.

        This is the one place live configuration is read. Everything downstream
        works from the snapshot this produces, so a version flip mid-flight cannot
        change what an in-flight run does.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT a.id AS actor_id, av.id AS version_id, av.version,
                           a.kind, av.spec, av.spec_hash, a.delegation
                      FROM actors a
                      JOIN actor_versions av ON av.id = a.active_version_id
                     WHERE a.organization_id = :org AND a.name = :name
                    """
                ),
                {"org": organization_id, "name": name},
            )
        ).one_or_none()
        if row is None:
            raise UnknownActorError(f"no active version for actor {name!r}")
        return ResolvedActor(
            actor_id=ActorId(row.actor_id),
            actor_version_id=int(row.version_id),
            version=int(row.version),
            kind=row.kind,
            spec=row.spec,
            spec_hash=row.spec_hash,
            delegation=row.delegation,
        )

    async def set_delegation(
        self, organization_id: uuid.UUID, name: str, limits: dict[str, Any] | None
    ) -> None:
        """M5. Write an actor's delegation limits. Called by `apply`, and by nothing
        that executes a run.

        `None` clears them, which is what an actor whose `delegation:` block was
        removed from the YAML should get — the alternative is a limit that survives its
        own deletion, which is the config-plane bug M4 §11's edge case 87 is about.
        """
        await self._s.execute(
            text(
                """
                UPDATE actors SET delegation = CAST(:limits AS jsonb)
                 WHERE organization_id = :org AND name = :name
                """
            ),
            {
                "org": organization_id,
                "name": name,
                "limits": to_jsonb(limits) if limits is not None else None,
            },
        )
