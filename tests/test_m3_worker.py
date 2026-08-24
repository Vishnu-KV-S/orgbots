"""The memory worker, the intentions queue and the procedure counters.

The worker is where §6's *"never the hot path"* stops being a design statement and
becomes a delivery mechanism, so what is tested is the delivery: its own consumer group,
at-least-once with a database-side idempotency check, and ack-before-work so a poison
entry cannot be redelivered forever at MEMORY-class prices.

`IntentionService` and `ProcedureService` are here rather than in their own file because
both are small and both are about *not* building a second copy of machinery that already
exists — a second scheduler, a second promotion path. The tests are mostly about the
absence.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from runtime.domain.ids import OrganizationId, new_run_id
from runtime.events.stream import RedisStreams
from runtime.events.topics import TOPIC_RUN_SUCCEEDED
from runtime.memory.intentions import (
    MAX_HORIZON_DAYS,
    IntentionService,
    ProcedureService,
    ProcedureStep,
    steps_hash,
)
from runtime.memory.worker import MEMORY_GROUP, RUN_MARKER_PREFIX, MemoryWorker
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import build_memory_harness, facts_json, seed_actor, seed_org

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000ac001")


# --- the worker --------------------------------------------------------------------


async def test_the_worker_consumes_run_succeeded_and_writes_memories(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    version_id = await seed_actor(uow_factory, org, ACTOR, "research")
    harness = await build_memory_harness(uow_factory, settings)
    run_id = await _seed_run(uow_factory, org, ACTOR, version_id)

    streams = RedisStreams(settings)
    worker = MemoryWorker(uow_factory, streams, harness.service, settings=harness.settings)
    await worker.setup()
    try:
        harness.provider.queue(facts_json(("Acme pricing", "Acme charges $49 per seat")))
        await streams.publish(
            TOPIC_RUN_SUCCEEDED,
            f"{run_id}:SUCCESS",
            {
                "outbox_id": "1",
                "organization_id": str(org),
                "dedupe_key": f"{run_id}:SUCCESS",
                "payload": f'{{"run_id": "{run_id}", "status": "SUCCESS"}}',
            },
        )
        # Drain until *this* run has been handled, rather than asserting on a batch
        # count. `run.succeeded` is a shared stream and Redis is not truncated between
        # tests, so any other test that ran a real loop has left entries in it — a
        # `handled == 1` assertion passes or fails on what else happened to be queued,
        # which is a flake that only shows up in a full-suite run. It did.
        for _ in range(5):
            if await _memories_for(uow_factory, run_id):
                break
            await worker.drain(block_ms=200)

        assert await _memories_for(uow_factory, run_id) == 1
        assert worker.processed >= 1
    finally:
        await streams.close()


async def test_a_redelivered_entry_does_not_re_extract(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Redis is at-least-once and should be — an entry lost because a worker died
    mid-extraction is a run whose facts are gone with nothing recording the gap. So
    every delivery must be safe to repeat, and the second one must cost nothing."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    version_id = await seed_actor(uow_factory, org, ACTOR, "research")
    harness = await build_memory_harness(uow_factory, settings)
    run_id = await _seed_run(uow_factory, org, ACTOR, version_id)

    streams = RedisStreams(settings)
    worker = MemoryWorker(uow_factory, streams, harness.service, settings=harness.settings)
    await worker.setup()
    try:
        harness.provider.queue(facts_json(("Acme pricing", "Acme charges $49 per seat")))
        fields = {
            "outbox_id": "1",
            "organization_id": str(org),
            "dedupe_key": f"{run_id}:SUCCESS",
            "payload": f'{{"run_id": "{run_id}"}}',
        }
        await worker._handle(fields)
        calls_after_first = len(harness.provider.calls)

        await worker._handle(fields)
        assert worker.skipped == 1
        assert len(harness.provider.calls) == calls_after_first, (
            "a redelivery spent a second extraction on a run already processed"
        )

        assert await _memories_for(uow_factory, run_id) == 1
    finally:
        await streams.close()


async def test_a_run_that_yields_no_facts_is_marked_examined(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Without the marker, "extracted nothing" and "never looked" are the same state and
    the second re-processes on every redelivery. It is also how §12's *"did memory
    actually stop the re-derivation you predicted"* gets a denominator."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    version_id = await seed_actor(uow_factory, org, ACTOR, "analytics")
    harness = await build_memory_harness(uow_factory, settings)
    run_id = await _seed_run(uow_factory, org, ACTOR, version_id)

    streams = RedisStreams(settings)
    worker = MemoryWorker(uow_factory, streams, harness.service, settings=harness.settings)
    try:
        harness.provider.queue('{"facts": []}')
        await worker._handle({"organization_id": str(org), "payload": f'{{"run_id": "{run_id}"}}'})
        async with uow_factory() as uow:
            marker = (
                await uow.session.execute(
                    text("SELECT memory_id, detail FROM memory_audit WHERE run_id = :r"),
                    {"r": run_id},
                )
            ).one()
        assert marker.memory_id == f"{RUN_MARKER_PREFIX}{run_id}"
        assert marker.detail["marker"] is True
        assert await worker._already_processed(run_id) is True
    finally:
        await streams.close()


async def test_a_poison_entry_is_acked_and_counted_rather_than_retried_forever(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Ack before work — the opposite of the relay's ordering, and right here.

    The alternative is a run whose extraction reliably raises being redelivered forever
    at MEMORY-class prices. §13 risk 4 with a multiplier. A dropped memory is a gap in
    `v_memory_inventory`; a retry storm is a bill.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    streams = RedisStreams(settings)
    worker = MemoryWorker(uow_factory, streams, harness.service, settings=harness.settings)
    await worker.setup()
    try:
        # A unique dedupe key per run. `RedisStreams.publish` dedupes on it through a
        # Lua script and Redis is not truncated between tests — a fixed key here makes
        # the second run of this file a no-op and the assertion below a mystery.
        await streams.publish(
            TOPIC_RUN_SUCCEEDED,
            f"poison-{uuid.uuid4()}",
            {"organization_id": str(org), "payload": "{not json at all"},
        )
        # Same caution as above: other tests share this stream, so what is asserted is
        # that the poison entry was *counted as failed* and not that it was the only
        # thing in the batch.
        for _ in range(5):
            await worker.drain(block_ms=200)
            if worker.failed:
                break
        assert worker.failed == 1

        # Acked: nothing this consumer took is still pending for it.
        assert await worker.drain(block_ms=100) == 0
    finally:
        await streams.close()


async def test_the_worker_group_is_separate_from_the_run_workers(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A consumer group is a *partition* of the stream. Joining `workers` would mean
    every entry the memory worker claimed was one the run workers never saw."""
    streams = RedisStreams(settings)
    try:
        await streams.ensure_group(TOPIC_RUN_SUCCEEDED, MEMORY_GROUP)
        groups = await streams.client.xinfo_groups(streams.stream_key(TOPIC_RUN_SUCCEEDED))
        names = {g["name"].decode() if isinstance(g["name"], bytes) else g["name"] for g in groups}
        assert MEMORY_GROUP in names
        assert settings.consumer_group != MEMORY_GROUP
    finally:
        await streams.close()


async def test_the_worker_does_not_start_when_memory_is_disabled(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`RUNTIME_MEMORY_ENABLED=false` must mean the M2 configuration exactly."""
    harness = await build_memory_harness(uow_factory, settings, memory_enabled=False)
    streams = RedisStreams(settings)
    worker = MemoryWorker(uow_factory, streams, harness.service, settings=harness.settings)
    try:
        await worker.run_forever()  # returns immediately rather than looping
        assert worker.processed == 0
    finally:
        await streams.close()


# --- intentions --------------------------------------------------------------------


async def test_an_intention_is_deduped_while_it_is_pending(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An actor re-deriving the same intention on every run accumulates one row per run
    until Thursday arrives with forty identical reminders."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    service = IntentionService(uow_factory)
    due = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)

    first = await service.schedule(
        org, actor_name="research", intent="Revisit Acme's pricing page", due_at=due
    )
    second = await service.schedule(
        org, actor_name="research", intent="revisit   ACME's pricing page", due_at=due
    )
    assert first is not None
    assert second is None, "the dedupe key is not normalising case and whitespace"


async def test_an_intention_beyond_the_horizon_is_refused(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An actor asked to remember something "eventually" picks a date, and a date six
    years out is a row that sits in the pending index forever and is never wrong."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    service = IntentionService(uow_factory)

    too_far = dt.datetime.now(dt.UTC) + dt.timedelta(days=MAX_HORIZON_DAYS + 1)
    assert await service.schedule(org, actor_name="r", intent="someday", due_at=too_far) is None

    past = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    assert await service.schedule(org, actor_name="r", intent="yesterday", due_at=past) is None


async def test_due_intentions_are_claimed_once(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An intention usually becomes a task, and two tasks is two runs and two bills for
    one decision — so the claim is `FOR UPDATE SKIP LOCKED`, like the dispatcher's."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    service = IntentionService(uow_factory)
    due = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)
    intention_id = await service.schedule(
        org, actor_name="research", intent="check pricing", due_at=due
    )
    assert intention_id is not None

    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2)
    claimed = await service.claim_due(now=later)
    assert [i.id for i in claimed] == [intention_id]

    await service.mark_fired(intention_id, run_id=new_run_id(), task_id=None)
    assert await service.claim_due(now=later) == []

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text("SELECT status, fired_run_id FROM scheduled_intentions WHERE id = :i"),
                {"i": intention_id},
            )
        ).one()
    assert row.status == "FIRED"
    assert row.fired_run_id is not None, (
        "the loop must close: 'did the thing we promised to revisit get revisited' is "
        "one of §1's hypotheses and it has to be a join"
    )


# --- procedures --------------------------------------------------------------------


def test_the_steps_hash_ignores_arguments() -> None:
    """Arguments differ every time; the shape is what recurs. Hashing arguments in would
    mean nothing ever recurred and the table filled with singletons — a feature doing
    nothing, quietly."""
    a = [ProcedureStep("search", "web.search@1"), ProcedureStep("fetch", "web.fetch@1")]
    b = [ProcedureStep("search", "web.search@1"), ProcedureStep("fetch", "web.fetch@1")]
    c = [ProcedureStep("search", "web.search@1")]
    assert steps_hash(a) == steps_hash(b)
    assert steps_hash(a) != steps_hash(c)


async def test_observations_and_outcomes_accumulate_on_one_row(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    service = ProcedureService(uow_factory)
    steps = [ProcedureStep("search", "web.search@1"), ProcedureStep("fetch", "web.fetch@1")]

    for succeeded in (True, True, False):
        await service.observe(
            org,
            actor_name="research",
            title="search then fetch",
            steps=steps,
            run_id=new_run_id(),
            succeeded=succeeded,
        )

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT observed_count, success_count, failure_count, status, "
                    "last_failure_run_id FROM procedure_candidates WHERE organization_id = :o"
                ),
                {"o": org},
            )
        ).one()
    assert (row.observed_count, row.success_count, row.failure_count) == (3, 2, 1)
    assert row.status == "OBSERVING", "nothing is adopted without a review (§8)"
    assert row.last_failure_run_id is not None, (
        "the interesting review question is when it stopped working, not whether it ever did"
    )

    ready = await service.ready_for_review(org, min_observations=3, min_successes=2)
    assert len(ready) == 1
    assert await service.ready_for_review(org, min_observations=5, min_successes=2) == []


async def test_a_procedure_cannot_claim_adoption_without_a_memory(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`ck_procedure_adopted_has_memory`. ADOPTED means a promotion happened, and a
    promotion produces a memory; a row claiming adoption with nothing to point at is
    the shape of an auto-promotion that skipped the queue."""
    from sqlalchemy.exc import IntegrityError

    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    service = ProcedureService(uow_factory)
    await service.observe(
        org,
        actor_name="research",
        title="t",
        steps=[ProcedureStep("n", "web.fetch@1")],
        run_id=None,
        succeeded=True,
    )
    with pytest.raises(IntegrityError, match="ck_procedure_adopted_has_memory"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    "UPDATE procedure_candidates SET status = 'ADOPTED' WHERE organization_id = :o"
                ),
                {"o": org},
            )


async def test_a_runs_shape_is_reconstructed_from_the_effect_journal(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """From the journal rather than from a trace the graph writes: `effect_intents`
    already records every tool call with its node and ordinal, for a stronger reason.
    A graph reporting its own shape would be a second thing to keep in sync, and it
    would be the half that silently stops being updated."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    version_id = await seed_actor(uow_factory, org, ACTOR, "research")
    run_id = await _seed_run(
        uow_factory, org, ACTOR, version_id, tools=["web.search@1", "web.fetch@1"]
    )

    await ProcedureService(uow_factory).observe_run(org, run_id, succeeded=True)

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT steps, observed_count, actor_name FROM procedure_candidates "
                    "WHERE organization_id = :o"
                ),
                {"o": org},
            )
        ).one()
    assert [s["tool"] for s in row.steps["steps"]] == ["web.fetch@1", "web.search@1"]
    assert row.actor_name == "research"


# --- helpers ----------------------------------------------------------------------


async def _memories_for(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> int:
    async with uow_factory() as uow:
        return int(
            (
                await uow.session.execute(
                    text("SELECT count(*) FROM memory_metadata WHERE source_run_id = :r"),
                    {"r": run_id},
                )
            ).scalar_one()
        )


async def _seed_run(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    actor_id: uuid.UUID,
    version_id: int,
    *,
    tools: list[str] | None = None,
) -> uuid.UUID:
    run_id = new_run_id()
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                """
                INSERT INTO runs (id, organization_id, root_run_id, actor_id,
                                  actor_version_id, status, idempotency_key, thread_id)
                VALUES (:id, :org, :id, :actor, :ver, 'SUCCESS', :idem, :thread)
                """
            ),
            {
                "id": run_id,
                "org": organization_id,
                "actor": actor_id,
                "ver": version_id,
                "idem": f"m3w-{run_id}",
                "thread": str(run_id),
            },
        )
        await uow.session.execute(
            text(
                "INSERT INTO run_specs (run_id, spec, spec_hash) "
                "VALUES (:id, CAST(:spec AS jsonb), 'x')"
            ),
            {"id": run_id, "spec": '{"correlation_id": null, "budget_pool_id": null}'},
        )
        await uow.session.execute(
            text(
                "INSERT INTO events (organization_id, run_id, root_run_id, topic, payload, "
                "dedupe_key) VALUES (:org, :run, :run, 'run.succeeded', "
                "CAST(:payload AS jsonb), :dedupe)"
            ),
            {
                "org": organization_id,
                "run": run_id,
                "payload": '{"output": {"report": {"summary": "done"}}}',
                "dedupe": f"{run_id}:SUCCESS",
            },
        )
        for i, tool in enumerate(tools or []):
            await uow.session.execute(
                text(
                    """
                    INSERT INTO effect_intents (id, logical_call_id, organization_id, run_id,
                        root_run_id, fence, node, ordinal, args_hash, tool_name, tool_version,
                        status, recovery_policy, blast_radius)
                    VALUES (:id, :lcid, :org, :run, :run, 1, :node, :ord, 'h', :tool, 1,
                            'COMMITTED', 'replay_safe', 'read')
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "lcid": f"{run_id}:{i}",
                    "org": organization_id,
                    "run": run_id,
                    "node": tool.split(".")[1].split("@")[0],
                    "ord": i,
                    "tool": tool,
                },
            )
    return run_id
