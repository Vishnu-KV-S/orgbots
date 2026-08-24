"""Object store backends.

Two implementations and one rule: a write either durably stored the bytes or
raised. There is no third outcome, and in particular there is no "probably fine"
path that lets a caller record a reference to an object that does not exist.

`boto3` is synchronous, so S3 calls run in a worker thread. That is not a
compromise for M0's throughput — the alternative is another dependency whose
failure modes we would then have to learn — but it is the reason `put`/`get` take
a thread hop.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol

from runtime.domain.errors import ArtifactNotFound, ArtifactWriteFailed
from runtime.settings import Settings


def content_key(organization_id: str, sha256: str) -> str:
    """Content-addressed, sharded two levels so no prefix gets hot.

    Content addressing means writing the same bytes twice is idempotent, which
    matters on replay: a re-executed tool that produces the same output overwrites
    itself with identical content rather than creating a second object.
    """
    return f"{organization_id}/{sha256[:2]}/{sha256[2:4]}/{sha256}"


class ObjectBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...


class FilesystemBackend:
    """Local-disk backend for the dockerless dev stack and for tests.

    Writes go to a temporary file and are renamed into place, so a crash mid-write
    leaves a temp file rather than a truncated object that reads as valid.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        _ = content_type

        def _write() -> str:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            return f"file://{path}"

        try:
            return await asyncio.to_thread(_write)
        except OSError as exc:
            raise ArtifactWriteFailed(f"filesystem write failed for {key}: {exc}") from exc

    async def get(self, key: str) -> bytes:
        def _read() -> bytes:
            return self._path(key).read_bytes()

        try:
            return await asyncio.to_thread(_read)
        except OSError as exc:
            raise ArtifactNotFound(f"no object at {key}") from exc

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)


class S3Backend:
    """MinIO or S3."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._bucket = settings.artifact_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            # Fail closed. A run that cannot store its output has not succeeded,
            # and reporting success here is how you get a green run with nothing
            # behind the artifact link.
            raise ArtifactWriteFailed(f"s3 write failed for {key}: {exc}") from exc
        return f"s3://{self._bucket}/{key}"

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            raise ArtifactNotFound(f"no object at s3://{self._bucket}/{key}") from exc

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except Exception:
                return False
            return True

        return await asyncio.to_thread(_head)


def build_backend(settings: Settings) -> ObjectBackend:
    if settings.artifact_backend == "fs":
        return FilesystemBackend(settings.artifact_fs_root)
    return S3Backend(settings)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
