"""The worker loop.

Two sources of work, and the redundancy is the point:

- **The Redis stream** is the fast path. A run announced by the outbox relay
  reaches a worker in milliseconds.
- **A Postgres poll** is the correct path. It asks the database directly for
  claimable runs.

If Redis is the only source, a `FLUSHALL` loses every queued run permanently —
the outbox rows are already marked published, so nothing republishes them. That is
R1/T8, and the poll is what makes it a non-event: the runs are still QUEUED in
Postgres, and the next poll picks them up. Redis makes the system fast; Postgres
makes it correct.

A stream entry for a run that someone else already claimed is acked and dropped.
The claim, not the stream, decides who owns a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from runtime.artifacts.store import ArtifactStore
from runtime.budget.service import BudgetService
from runtime.domain.errors import MissingCredentials
from runtime.domain.ids import RunId, WorkerId, new_worker_id
from runtime.effects.journal import EffectJournal
from runtime.events.stream import RedisStreams
from runtime.gateway.builtin import build_registry
from runtime.gateway.credentials import CredentialBroker, CredentialCipher
from runtime.gateway.governance import PermissionCache
from runtime.gateway.models import ModelGateway
from runtime.gateway.providers import build_providers
from runtime.gateway.ratelimit import RateLimiter
from runtime.gateway.tools import ToolGateway
from runtime.memory import MemorySubsystem
from runtime.observability.logging import get_logger
from runtime.org.killswitch import KillSwitchService
from runtime.org.services import OrgServices, build_org_services
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.delegation import DelegationService
from runtime.runtime.run_service import RunService
from runtime.settings import Settings, get_settings
from runtime.worker.executor import RunExecutor, RunOutcome
from runtime.worker.lease import LeaseManager

log = get_logger("worker")

TOPIC_QUEUED = "run.queued"
STALE_ENTRY_MS = 60_000


@dataclass
class WorkerStats:
    claimed: int = 0
    skipped: int = 0
    succeeded: int = 0
    failed: int = 0


class Worker:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        streams: RedisStreams,
        *,
        settings: Settings | None = None,
        worker_id: WorkerId | None = None,
        checkpointer: object = None,
        providers: dict[str, Any] | None = None,
        memory: MemorySubsystem | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._uow = uow_factory
        self._streams = streams
        self.worker_id = worker_id or new_worker_id()
        self._leases = LeaseManager(uow_factory, self._settings)
        self._budget = BudgetService(reservation_ttl_seconds=self._settings.reservation_ttl_seconds)
        self._artifacts = ArtifactStore(uow_factory, settings=self._settings)
        registry = build_registry(uow_factory, self._settings)

        # --- M2 governance, constructed once and shared by both gateways -----------
        # Shared deliberately: each of these holds a TTL cache, and one cache per
        # gateway would double every read it exists to avoid — and, worse, would let
        # the two gateways disagree about whether a kill switch is engaged for up to
        # ten seconds. One incident, two answers.
        self._kill_switches = KillSwitchService(uow_factory)
        self._permissions = PermissionCache(uow_factory)
        self._rate_limiter = RateLimiter(uow_factory, streams.client)
        self._credentials = _build_credential_broker(uow_factory)

        self._tools = ToolGateway(
            registry,
            EffectJournal(uow_factory),
            uow_factory,
            artifacts=self._artifacts,
            budget=self._budget,
            lease_check=self._leases.check,
            settings=self._settings,
            kill_switches=self._kill_switches,
            permissions=self._permissions,
            rate_limiter=self._rate_limiter,
            credentials=self._credentials,
        )
        self._models = ModelGateway(
            uow_factory,
            providers=providers or build_providers(self._settings),
            budget=self._budget,
            settings=self._settings,
            lease_check=self._leases.check,
            kill_switches=self._kill_switches,
            rate_limiter=self._rate_limiter,
            credentials=self._credentials,
        )
        # A bot's long task carries on through the dispatcher, so only where one runs.
        self._org = build_org_services(
            uow_factory,
            self._artifacts,
            bot_chunks=(
                self._settings.bot_auto_continue_chunks if self._settings.conductor_enabled else 1
            ),
        )
        # --- M3 -------------------------------------------------------------------
        # Built here, when it is built at all, because it needs `self._models` — the
        # same gateway the graphs use, so extraction and retrieval are accounted through
        # the same pipeline and appear in the same denial stream. `worker.main` passes
        # one in so that the worker process shares a single subsystem (and therefore a
        # single embedding cache) with `MemoryWorker`; a test that does not pass one
        # gets `None` and the M2 behaviour, exactly.
        self._memory = memory
        # --- M5 -------------------------------------------------------------------
        # Constructed only when delegation is on, and it is `None` otherwise rather
        # than a disabled instance. That is the difference between a capability the
        # runtime does not have and one it has and refuses, and T68's claim is the
        # former: with the flag off there is no `RunService` inside this worker at all,
        # so no code path here can create a run.
        #
        # It holds its **own** `RunService` — a worker that shared the API's would be
        # sharing an `AuthorityResolver` cache across processes that cannot share one.
        # The budget service *is* shared, because it is stateless apart from the TTL
        # and because the subtree read and the reservation should come from one place.
        self._delegation: DelegationService | None = None
        if self._settings.delegation_enabled:
            self._delegation = DelegationService(
                uow_factory,
                RunService(uow_factory, settings=self._settings, budget=self._budget),
                settings=self._settings,
                budget=self._budget,
            )
        self._executor = RunExecutor(
            uow_factory,
            settings=self._settings,
            lease_manager=self._leases,
            tools=self._tools,
            models=self._models,
            artifacts=self._artifacts,
            checkpointer=checkpointer,
            org=self._org,
            memory=memory.planner if memory is not None else None,
            delegation=self._delegation,
        )
        self._stopping = asyncio.Event()
        self.stats = WorkerStats()

    @property
    def consumer_name(self) -> str:
        return f"worker-{self.worker_id}"

    @property
    def leases(self) -> LeaseManager:
        """Exposed so a caller can claim and execute as two separate steps — the
        chaos tests need to do something in between."""
        return self._leases

    @property
    def memory(self) -> MemorySubsystem | None:
        return self._memory

    def attach_memory(self, memory: MemorySubsystem) -> None:
        """Give an already-built worker its memory subsystem. M3.

        The construction order is genuinely circular otherwise: `MemorySubsystem` needs
        a `ModelGateway` so that extraction and retrieval are accounted through the same
        pipeline as the graphs, and the gateway worth using is this worker's own — built
        in `__init__` with its shared kill-switch, rate-limiter and credential broker.
        Building a second gateway for memory would give it a second TTL cache and a
        second opinion about whether a kill switch is engaged, which is the exact failure
        the comment above `_kill_switches` is about.

        So: build the worker, build the subsystem against `worker.executor.models`,
        attach. `worker.main` is the only caller.
        """
        self._memory = memory
        self._executor.attach_memory(memory.planner)

    @property
    def executor(self) -> RunExecutor:
        return self._executor

    @property
    def delegation(self) -> DelegationService | None:
        """`None` when delegation is off, which is the default. Exposed so a test can
        assert its absence as easily as its presence — T68 asserts the absence."""
        return self._delegation

    @property
    def org(self) -> OrgServices:
        return self._org

    async def setup(self) -> None:
        await self._streams.ensure_group(TOPIC_QUEUED)

    async def run_one(self, run_id: RunId) -> RunOutcome | None:
        """Claim and execute a single run. None if another worker owns it."""
        lease = await self._leases.claim(run_id, self.worker_id)
        if lease is None:
            self.stats.skipped += 1
            return None
        self.stats.claimed += 1
        outcome = await self._executor.execute(lease, self.worker_id)
        if outcome.status.value == "SUCCESS":
            self.stats.succeeded += 1
        elif outcome.status.value == "FAILED":
            self.stats.failed += 1
        return outcome

    async def drain_stream(self, *, block_ms: int = 100, count: int = 10) -> int:
        """Take work from Redis. Acks every entry it looks at, claimed or not.

        An entry for a run this worker could not claim is not a failure — someone
        else is on it — so holding the entry in the pending list would just make it
        someone's problem later.
        """
        handled = 0
        entries = await self._streams.read_pending(TOPIC_QUEUED, self.consumer_name, count=count)
        entries += await self._streams.claim_stale(
            TOPIC_QUEUED, self.consumer_name, min_idle_ms=STALE_ENTRY_MS, count=count
        )
        entries += await self._streams.read(
            TOPIC_QUEUED, self.consumer_name, count=count, block_ms=block_ms
        )

        for entry_id, fields in entries:
            run_id = _run_id_from(fields)
            if run_id is not None:
                with contextlib.suppress(Exception):
                    await self.run_one(run_id)
                handled += 1
            await self._streams.ack(TOPIC_QUEUED, [entry_id])
        return handled

    async def drain_database(self, limit: int = 8) -> int:
        """Take work straight from Postgres.

        This is the path that survives a Redis wipe. It is also the path that picks
        up a run whose stream entry was consumed by a worker that then died before
        claiming it.
        """
        async with self._uow() as uow:
            candidates = await uow.runs.claimable(limit)
        handled = 0
        for run_id in candidates:
            with contextlib.suppress(Exception):
                if await self.run_one(run_id) is not None:
                    handled += 1
        return handled

    async def run_forever(self) -> None:
        await self.setup()
        while not self._stopping.is_set():
            try:
                worked = await self.drain_stream(block_ms=200)
                worked += await self.drain_database()
            except Exception:
                log.exception("worker.loop_error")
                worked = 0
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=0.2)

    def stop(self) -> None:
        self._stopping.set()


def _run_id_from(fields: dict[str, str]) -> RunId | None:
    import uuid

    raw = fields.get("payload")
    if raw:
        with contextlib.suppress(json.JSONDecodeError, KeyError, ValueError):
            return RunId(uuid.UUID(json.loads(raw)["run_id"]))
    dedupe = fields.get("dedupe_key", "")
    with contextlib.suppress(ValueError):
        return RunId(uuid.UUID(dedupe.split(":")[0]))
    return None


def _build_credential_broker(uow_factory: UnitOfWorkFactory) -> CredentialBroker | None:
    """A broker if a key is configured, `None` if not — and a warning either way.

    Returning `None` rather than raising is the one concession this makes, and it is
    scoped: a runtime with no `RUNTIME_CREDENTIAL_KEYS` can still run every tool that
    needs no credential, which is most of them and all of the M0 test suite. A tool
    that *does* need one gets a clear refusal at the point of use naming the credential
    it wanted, rather than a process that would not start and a developer wondering
    which of forty tools was responsible.

    The warning is not decoration. A production worker running without a broker will
    fail the first publish of the week, and the log line is what makes that a
    five-minute fix instead of an investigation.
    """
    try:
        cipher = CredentialCipher.from_env()
    except (MissingCredentials, ValueError) as exc:
        log.warning(
            "credentials.disabled",
            reason=str(exc).splitlines()[0],
            impact="tools requiring a credential will be refused at the gateway",
        )
        return None
    broker = CredentialBroker(uow_factory, cipher)
    log.info("credentials.enabled", active_key_id=cipher.active_key_id)
    return broker
