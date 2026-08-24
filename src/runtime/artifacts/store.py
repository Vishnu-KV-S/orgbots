"""Artifact store.

The ordering in `put()` is the whole contract:

1. Write the bytes. If this fails, raise — nothing has been recorded, so nothing
   dangles.
2. Verify the bytes are there.
3. Only then write the metadata row.

Doing it the other way round produces a reference to an object that does not
exist, which is worse than a failure because it fails later, somewhere else, to
someone who did not cause it.

Fail-closed is the second half of the same idea and is what T12 checks: when the
object store is down, the run FAILS. It does not succeed with a missing output and
it does not silently inline the payload as a "graceful degradation" — that turns a
loud infrastructure problem into a quiet data problem.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.artifacts.backends import ObjectBackend, build_backend, content_key, sha256_hex
from runtime.domain.errors import ArtifactWriteFailed
from runtime.domain.ids import ArtifactId, OrganizationId, RunId, new_artifact_id
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("artifacts.store")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: ArtifactId
    version: int
    uri: str
    sha256: str
    size_bytes: int
    content_type: str

    def to_json(self) -> dict[str, object]:
        return {
            "artifact_id": str(self.artifact_id),
            "version": self.version,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


class ArtifactStore:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        settings: Settings | None = None,
        backend: ObjectBackend | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._backend = backend or build_backend(self._settings)

    @property
    def threshold_bytes(self) -> int:
        return self._settings.artifact_threshold_bytes

    def should_externalise(self, data: bytes) -> bool:
        return len(data) > self.threshold_bytes

    async def put(
        self,
        data: bytes,
        *,
        organization_id: OrganizationId,
        run_id: RunId | None,
        kind: str,
        content_type: str = "application/octet-stream",
        artifact_id: ArtifactId | None = None,
    ) -> ArtifactRef:
        """Store bytes and record them. Raises `ArtifactWriteFailed` on any doubt."""
        digest = sha256_hex(data)
        key = content_key(str(organization_id), digest)

        uri = await self._backend.put(key, data, content_type)

        if not await self._backend.exists(key):
            # The backend said it wrote and the object is not there. Refusing to
            # record the row is the only safe response.
            raise ArtifactWriteFailed(f"object {key} missing immediately after a successful put")

        resolved_id = artifact_id or new_artifact_id()
        async with self._uow.transaction() as uow:
            await uow.artifacts.create(resolved_id, organization_id, run_id, kind, content_type)
            version = await uow.artifacts.add_version(resolved_id, uri, len(data), digest)

        log.debug(
            "artifact.stored",
            artifact_id=str(resolved_id),
            version=version,
            size_bytes=len(data),
        )
        return ArtifactRef(
            artifact_id=resolved_id,
            version=version,
            uri=uri,
            sha256=digest,
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get(self, artifact_id: ArtifactId) -> bytes:
        async with self._uow() as uow:
            row = await uow.artifacts.latest(artifact_id)
        data = await self._backend.get(content_key(_org_from_uri(row.uri), row.sha256))
        if sha256_hex(data) != row.sha256:
            raise ArtifactWriteFailed(
                f"artifact {artifact_id} v{row.version} does not match its recorded digest"
            )
        return data

    async def link(
        self, artifact_id: ArtifactId, *, source_type: str, source_id: str, relation: str
    ) -> None:
        async with self._uow.transaction() as uow:
            await uow.artifacts.link(artifact_id, source_type, source_id, relation)


def _org_from_uri(uri: str) -> str:
    """Recover the organization prefix from a stored URI.

    The key layout is `{org}/{aa}/{bb}/{sha}`, so the org is the first path
    segment after the scheme and (for s3) the bucket.
    """
    without_scheme = uri.split("://", 1)[-1]
    parts = without_scheme.split("/")
    if uri.startswith("s3://"):
        parts = parts[1:]
    # A filesystem URI carries the store root ahead of the key; the org is the
    # segment four from the end.
    return parts[-4] if len(parts) >= 4 else parts[0]
