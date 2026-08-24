"""T2 — start_run() is atomic and idempotent.

The interesting assertion is not "a repeat returns the same run". It is that
100 genuinely concurrent callers with the same idempotency key produce exactly one
run row, exactly one spec row, exactly one outbox row and exactly one reservation
— and that the 99 losers wrote nothing at all.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import ActorKind, RunStatus, WorkClass
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import (
    ActorSpec,
    Ceilings,
    ModelProfile,
    ModelProfiles,
    StartRunRequest,
)
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService
from runtime.settings import Settings

pytestmark = pytest.mark.integration

ECHO_SPEC = ActorSpec(
    name="echo-agent",
    kind=ActorKind.LLM_AGENT,
    graph_ref="echo_agent@1",
    allowed_tools=frozenset({"web.fetch@1", "fixture.sideeffect@1"}),
    ceilings=Ceilings(max_llm_calls=4, max_tool_calls=8),
    model_profiles=ModelProfiles(
        profiles={WorkClass.GENERATION: ModelProfile(provider="fake", model="echo-1")}
    ),
)


@pytest_asyncio.fixture
async def seeded(uow_factory: UnitOfWorkFactory, organization_id: OrganizationId) -> OrganizationId:
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, "acme")
    await registrar.publish_actor(organization_id, ECHO_SPEC)
    return organization_id


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory, settings: Settings) -> RunService:
    return RunService(uow_factory, settings=settings)


async def test_start_run_writes_everything_in_one_transaction(
    service: RunService, uow_factory: UnitOfWorkFactory, seeded: OrganizationId
) -> None:
    result = await service.start_run(
        StartRunRequest(
            organization_id=seeded,
            actor_name="echo-agent",
            input={"message": "hello"},
            idempotency_key="k1",
        )
    )
    assert result.created is True
    assert result.status is RunStatus.QUEUED

    async with uow_factory() as uow:
        run = await uow.runs.get(result.run_id)
        assert run is not None
        assert run.status == "QUEUED"
        assert run.fence == 0
        assert run.thread_id == str(result.run_id)
        assert run.root_run_id == result.run_id

        spec_row = await uow.runs.get_spec(result.run_id)
        assert spec_row is not None
        assert spec_row.spec_hash == result.spec_hash

        outbox = (
            await uow.session.execute(
                text("SELECT topic, dedupe_key FROM outbox WHERE dedupe_key = :k"),
                {"k": str(result.run_id)},
            )
        ).all()
        assert [(r.topic, r.dedupe_key) for r in outbox] == [("run.queued", str(result.run_id))]

        reservations = (
            await uow.session.execute(
                text("SELECT count(*) FROM budget_reservations WHERE run_id = :r"),
                {"r": result.run_id},
            )
        ).scalar_one()
        assert reservations == 1


async def test_the_frozen_spec_is_what_the_worker_will_load(
    service: RunService, uow_factory: UnitOfWorkFactory, seeded: OrganizationId
) -> None:
    """Editing the actor after admission must not change the in-flight run."""
    result = await service.start_run(
        StartRunRequest(organization_id=seeded, actor_name="echo-agent", idempotency_key="k2")
    )
    await Registrar(uow_factory).publish_actor(
        seeded, ECHO_SPEC.model_copy(update={"ceilings": Ceilings(max_tool_calls=999)})
    )
    async with uow_factory() as uow:
        run_spec = await service.get_run_spec(uow, result.run_id)
    assert run_spec is not None
    assert run_spec.ceilings.max_tool_calls == 8


async def test_repeat_returns_the_existing_run_unchanged(
    service: RunService, seeded: OrganizationId
) -> None:
    first = await service.start_run(
        StartRunRequest(organization_id=seeded, actor_name="echo-agent", idempotency_key="same")
    )
    second = await service.start_run(
        StartRunRequest(
            organization_id=seeded,
            actor_name="echo-agent",
            input={"different": "input"},
            idempotency_key="same",
        )
    )
    assert second.created is False
    assert second.run_id == first.run_id
    assert second.spec_hash == first.spec_hash


async def test_100_concurrent_identical_keys_produce_exactly_one_run(
    service: RunService, uow_factory: UnitOfWorkFactory, seeded: OrganizationId
) -> None:
    """T2. The whole point of `uq_run_idem`."""
    request = StartRunRequest(
        organization_id=seeded, actor_name="echo-agent", idempotency_key="stampede"
    )
    results = await asyncio.gather(
        *(service.start_run(request) for _ in range(100)), return_exceptions=True
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"start_run raised under contention: {failures[:3]}"

    created = [r for r in results if not isinstance(r, BaseException) and r.created]
    assert len(created) == 1, f"{len(created)} callers believed they created the run"

    run_ids = {r.run_id for r in results if not isinstance(r, BaseException)}
    assert len(run_ids) == 1

    async with uow_factory() as uow:
        counts = (
            await uow.session.execute(
                text(
                    """
                    SELECT (SELECT count(*) FROM runs) AS runs,
                           (SELECT count(*) FROM run_specs) AS specs,
                           (SELECT count(*) FROM outbox) AS outbox,
                           (SELECT count(*) FROM budget_reservations) AS reservations
                    """
                )
            )
        ).one()
    assert (counts.runs, counts.specs, counts.outbox, counts.reservations) == (1, 1, 1, 1)


async def test_different_keys_produce_different_runs(
    service: RunService, seeded: OrganizationId
) -> None:
    results = await asyncio.gather(
        *(
            service.start_run(
                StartRunRequest(
                    organization_id=seeded, actor_name="echo-agent", idempotency_key=f"k{i}"
                )
            )
            for i in range(20)
        )
    )
    assert len({r.run_id for r in results}) == 20
    assert all(r.created for r in results)


async def test_unknown_actor_is_refused(service: RunService, seeded: OrganizationId) -> None:
    from runtime.domain.errors import UnknownActorError

    with pytest.raises(UnknownActorError):
        await service.start_run(
            StartRunRequest(
                organization_id=seeded, actor_name="does-not-exist", idempotency_key="k"
            )
        )


async def test_a_failed_admission_writes_nothing(
    service: RunService, uow_factory: UnitOfWorkFactory, seeded: OrganizationId
) -> None:
    """Atomicity in the negative direction: a refused run leaves no debris."""
    from runtime.domain.errors import UnknownActorError

    with pytest.raises(UnknownActorError):
        await service.start_run(
            StartRunRequest(organization_id=seeded, actor_name="nope", idempotency_key="k")
        )
    async with uow_factory() as uow:
        for table in ("runs", "run_specs", "outbox", "events", "budget_reservations"):
            count = (await uow.session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert count == 0, f"{table} has {count} rows after a refused admission"


async def test_hybrid_actor_cannot_be_admitted(
    service: RunService, uow_factory: UnitOfWorkFactory, seeded: OrganizationId
) -> None:
    """T10 — HYBRID raises at compile, which for a run means at admission."""
    async with uow_factory.transaction() as uow:
        # Bypass ActorSpec validation to plant a HYBRID spec, which is the only way
        # one could reach the compiler in the first place.
        await uow.session.execute(
            text(
                """
                UPDATE actor_versions SET spec = jsonb_set(spec, '{kind}', '"hybrid"')
                 WHERE actor_id = (SELECT id FROM actors WHERE name = 'echo-agent')
                """
            )
        )
    with pytest.raises(NotImplementedError, match="HYBRID"):
        await service.start_run(
            StartRunRequest(
                organization_id=seeded, actor_name="echo-agent", idempotency_key="hybrid"
            )
        )
