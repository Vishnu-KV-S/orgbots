"""Artifact metadata.

The bytes live in the object store; these rows are the index into it. A version row
is written only after the bytes are durable — see `ArtifactStore.put`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.errors import ArtifactNotFound
from runtime.domain.ids import ArtifactId


@dataclass(frozen=True, slots=True)
class ArtifactVersionRow:
    artifact_id: ArtifactId
    version: int
    uri: str
    size_bytes: int
    sha256: str
    content_type: str


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        artifact_id: ArtifactId,
        organization_id: uuid.UUID,
        run_id: uuid.UUID | None,
        kind: str,
        content_type: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO artifacts (id, organization_id, run_id, kind, content_type)
                VALUES (:id, :org, :run_id, :kind, :ct)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": artifact_id,
                "org": organization_id,
                "run_id": run_id,
                "kind": kind,
                "ct": content_type,
            },
        )

    async def add_version(
        self, artifact_id: ArtifactId, uri: str, size_bytes: int, sha256: str
    ) -> int:
        """Append a version and advance `current_version`, atomically.

        The version number comes from the row itself rather than from the caller so
        two concurrent writers cannot both decide they are version 2.
        """
        version = (
            await self._s.execute(
                text(
                    """
                    UPDATE artifacts SET current_version = current_version + 1
                     WHERE id = :id RETURNING current_version
                    """
                ),
                {"id": artifact_id},
            )
        ).scalar_one_or_none()
        if version is None:
            raise ArtifactNotFound(f"artifact {artifact_id} does not exist")
        await self._s.execute(
            text(
                """
                INSERT INTO artifact_versions (artifact_id, version, uri, size_bytes, sha256)
                VALUES (:id, :v, :uri, :size, :sha)
                """
            ),
            {"id": artifact_id, "v": version, "uri": uri, "size": size_bytes, "sha": sha256},
        )
        return int(version)

    async def latest(self, artifact_id: ArtifactId) -> ArtifactVersionRow:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT av.artifact_id, av.version, av.uri, av.size_bytes, av.sha256,
                           a.content_type
                      FROM artifact_versions av
                      JOIN artifacts a ON a.id = av.artifact_id
                     WHERE av.artifact_id = :id
                     ORDER BY av.version DESC LIMIT 1
                    """
                ),
                {"id": artifact_id},
            )
        ).one_or_none()
        if row is None:
            raise ArtifactNotFound(f"artifact {artifact_id} has no versions")
        return ArtifactVersionRow(
            artifact_id=ArtifactId(row.artifact_id),
            version=row.version,
            uri=row.uri,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            content_type=row.content_type,
        )

    async def link(
        self, artifact_id: ArtifactId, source_type: str, source_id: str, relation: str
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO artifact_links (artifact_id, source_type, source_id, relation)
                VALUES (:id, :st, :sid, :rel)
                ON CONFLICT ON CONSTRAINT uq_artifact_link DO NOTHING
                """
            ),
            {"id": artifact_id, "st": source_type, "sid": source_id, "rel": relation},
        )

    async def for_run(self, run_id: uuid.UUID) -> list[ArtifactVersionRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT DISTINCT ON (av.artifact_id)
                           av.artifact_id, av.version, av.uri, av.size_bytes, av.sha256,
                           a.content_type
                      FROM artifact_versions av
                      JOIN artifacts a ON a.id = av.artifact_id
                     WHERE a.run_id = :run_id
                     ORDER BY av.artifact_id, av.version DESC
                    """
                ),
                {"run_id": run_id},
            )
        ).all()
        return [
            ArtifactVersionRow(
                artifact_id=ArtifactId(r.artifact_id),
                version=r.version,
                uri=r.uri,
                size_bytes=r.size_bytes,
                sha256=r.sha256,
                content_type=r.content_type,
            )
            for r in rows
        ]
