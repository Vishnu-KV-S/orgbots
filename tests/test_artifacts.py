"""T12 — the artifact store fails closed.

When the object store is down, the run FAILS. It does not report success with a
missing output, and it does not "gracefully degrade" by inlining the payload —
that would turn a loud infrastructure problem into a quiet data problem that
surfaces weeks later as an artifact link pointing at nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.artifacts.backends import ObjectBackend, sha256_hex
from runtime.artifacts.store import ArtifactStore
from runtime.domain.errors import ArtifactWriteFailed
from runtime.domain.ids import OrganizationId
from runtime.settings import Settings
from tests.conftest_runtime import Runtime, build_runtime

pytestmark = pytest.mark.integration


class ExplodingBackend:
    """Stands in for MinIO returning 500."""

    def __init__(self, *, fail_on_put: bool = True, lie_about_success: bool = False) -> None:
        self.fail_on_put = fail_on_put
        self.lie_about_success = lie_about_success
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        if self.fail_on_put:
            raise ArtifactWriteFailed(f"s3 write failed for {key}: 500 Internal Error")
        if not self.lie_about_success:
            self.objects[key] = data
        return f"s3://bucket/{key}"

    async def get(self, key: str) -> bytes:
        from runtime.domain.errors import ArtifactNotFound

        try:
            return self.objects[key]
        except KeyError as exc:
            raise ArtifactNotFound(key) from exc

    async def exists(self, key: str) -> bool:
        return key in self.objects


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


async def test_a_failed_write_raises_and_records_nothing(rt: Runtime) -> None:
    """T12, at the store: no metadata row for bytes that are not there."""
    store = ArtifactStore(rt.uow, settings=rt.settings, backend=ExplodingBackend())

    with pytest.raises(ArtifactWriteFailed, match="500"):
        await store.put(
            b"payload",
            organization_id=rt.organization_id,
            run_id=None,
            kind="test",
            content_type="application/json",
        )

    async with rt.uow() as uow:
        count = (await uow.session.execute(text("SELECT count(*) FROM artifacts"))).scalar_one()
    assert count == 0, "a metadata row was written for an object that does not exist"


async def test_a_backend_that_lies_about_success_is_caught(rt: Runtime) -> None:
    """The post-write existence check. A backend that returns 200 and stores
    nothing is rarer than one that returns 500 — and far more damaging, because
    nothing else in the system would ever notice."""
    backend = ExplodingBackend(fail_on_put=False, lie_about_success=True)
    store = ArtifactStore(rt.uow, settings=rt.settings, backend=backend)

    with pytest.raises(ArtifactWriteFailed, match="missing immediately after"):
        await store.put(
            b"payload",
            organization_id=rt.organization_id,
            run_id=None,
            kind="test",
        )

    async with rt.uow() as uow:
        count = (await uow.session.execute(text("SELECT count(*) FROM artifacts"))).scalar_one()
    assert count == 0


async def test_a_run_whose_artifact_write_fails_ends_as_failed(rt: Runtime) -> None:
    """T12, end to end: the run must FAIL, not succeed with no output."""
    rt.worker.executor.tools._artifacts = ArtifactStore(
        rt.uow, settings=rt.settings, backend=ExplodingBackend()
    )
    # Force externalisation by dropping the threshold below any real payload.
    rt.worker.executor.tools._artifacts._settings = rt.settings.model_copy(
        update={"artifact_threshold_bytes": 1}
    )

    started = await rt.start("echo-agent", "t12-fail", {"sideeffect": {"big": "x" * 100}})
    await rt.pump()

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
        effects = await uow.effects.for_run(started.run_id)
    assert run is not None
    assert run.status == "FAILED", "the run reported success despite losing its output"
    assert "ArtifactWriteFailed" in (run.status_reason or "")
    assert effects[0].status.value == "INTENT", (
        "the effect must stay INTENT: it happened, but we could not record its "
        "result, and pretending otherwise is how a replay decides to re-fire"
    )


async def test_a_large_tool_result_becomes_an_artifact(rt: Runtime) -> None:
    """The happy path for the same mechanism."""
    # The tool's *result* is what gets externalised, not its input, and
    # `fixture.sideeffect` returns a small one. Drop the threshold below it rather
    # than inflating the payload, so the test states which side of the call it is
    # measuring.
    rt.worker.executor.tools._artifacts._settings = rt.settings.model_copy(
        update={"artifact_threshold_bytes": 8}
    )

    started = await rt.start("echo-agent", "t12-large", {"sideeffect": {"blob": "y" * 500}})
    await rt.pump()

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
        effects = await uow.effects.for_run(started.run_id)
        versions = await uow.artifacts.for_run(started.run_id)
        links = (
            await uow.session.execute(text("SELECT source_type, relation FROM artifact_links"))
        ).all()
    assert run is not None
    assert run.status == "SUCCESS"
    assert effects[0].result_ref is not None, "the journal must point at the artifact"
    assert effects[0].result_inline is None, "and must not also inline the payload"
    assert len(versions) == 1
    assert versions[0].size_bytes > 8
    assert [(r.source_type, r.relation) for r in links] == [("effect", "produced")]


async def test_a_small_result_stays_inline(rt: Runtime) -> None:
    started = await rt.start("echo-agent", "t12-small", {"sideeffect": {"n": 1}})
    await rt.pump()

    async with rt.uow() as uow:
        effects = await uow.effects.for_run(started.run_id)
        artifacts = (await uow.session.execute(text("SELECT count(*) FROM artifacts"))).scalar_one()
    assert effects[0].result_inline is not None
    assert effects[0].result_ref is None
    assert artifacts == 0


async def test_stored_bytes_round_trip_and_are_digest_checked(rt: Runtime) -> None:
    store = ArtifactStore(rt.uow, settings=rt.settings)
    data = json.dumps({"hello": "world"}).encode()

    ref = await store.put(
        data,
        organization_id=rt.organization_id,
        run_id=None,
        kind="test",
        content_type="application/json",
    )
    assert ref.sha256 == sha256_hex(data)
    assert await store.get(ref.artifact_id) == data


async def test_writing_the_same_bytes_twice_is_idempotent_in_the_object_store(
    rt: Runtime,
) -> None:
    """Content addressing: a re-executed tool that produces identical output
    overwrites itself rather than creating a second object."""
    store = ArtifactStore(rt.uow, settings=rt.settings)
    data = b"identical"

    first = await store.put(data, organization_id=rt.organization_id, run_id=None, kind="t")
    second = await store.put(data, organization_id=rt.organization_id, run_id=None, kind="t")
    assert first.sha256 == second.sha256
    assert first.uri == second.uri


def test_the_backend_protocol_is_satisfied_by_the_test_double() -> None:
    backend: ObjectBackend = ExplodingBackend()
    assert backend is not None
