"""Unit of work.

`start_run()` writes to five tables and all five writes are one transaction or the
run is a lie. This is the object that makes that a single `async with` rather than
five hopeful calls.

The repositories are attributes rather than separately constructed, so there is no
way to accidentally hold a repository bound to a different session than the one
being committed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.persistence.engine import session_factory
from runtime.persistence.repositories.actors import ActorRepository
from runtime.persistence.repositories.approvals import ApprovalRepository
from runtime.persistence.repositories.artifacts import ArtifactRepository
from runtime.persistence.repositories.audit import AuditRepository
from runtime.persistence.repositories.authority import AuthorityRepository
from runtime.persistence.repositories.budget import BudgetRepository
from runtime.persistence.repositories.credentials import CredentialRepository
from runtime.persistence.repositories.delegations import DelegationRepository
from runtime.persistence.repositories.departments import DepartmentRepository
from runtime.persistence.repositories.effects import EffectRepository
from runtime.persistence.repositories.inbox import InboxRepository
from runtime.persistence.repositories.killswitch import KillSwitchRepository
from runtime.persistence.repositories.memory import (
    ContextTraceRepository,
    EntityRepository,
    IntentionRepository,
    MemoryMetadataRepository,
    ProcedureRepository,
    PromotionRepository,
)
from runtime.persistence.repositories.org import GoalRepository, MetricsRepository
from runtime.persistence.repositories.outbox import OutboxRepository
from runtime.persistence.repositories.ratelimits import RateLimitRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository
from runtime.persistence.repositories.spec import SpecRepository
from runtime.persistence.repositories.tasks import EvaluationRepository, TaskRepository
from runtime.persistence.repositories.triggers import TriggerRepository
from runtime.settings import Settings


class UnitOfWork:
    """One transaction, one session, every repository bound to it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.actors = ActorRepository(session)
        self.runs = RunRepository(session)
        self.outbox = OutboxRepository(session)
        self.effects = EffectRepository(session)
        self.artifacts = ArtifactRepository(session)
        self.budget = BudgetRepository(session)
        self.audit = AuditRepository(session)
        # M1. Same rule as above: bound to this session, never constructed
        # separately, so nothing can be committed against a different transaction
        # than the one it was read in.
        self.goals = GoalRepository(session)
        self.tasks = TaskRepository(session)
        self.evaluations = EvaluationRepository(session)
        self.inbox = InboxRepository(session)
        self.sessions = SessionRepository(session)
        self.triggers = TriggerRepository(session)
        self.approvals = ApprovalRepository(session)
        self.metrics = MetricsRepository(session)
        # M2. Same rule again. `authority` and `killswitch` are read on the admission
        # and gateway paths respectively, and both are read through a TTL cache that
        # lives in the service above — the repository stays a plain read so the cache
        # is one thing in one place rather than a behaviour smeared across two.
        self.authority = AuthorityRepository(session)
        self.killswitch = KillSwitchRepository(session)
        self.credentials = CredentialRepository(session)
        self.rate_limits = RateLimitRepository(session)
        # M3. `memories` is the sidecar and `traces` is the shadow-mode evidence; both
        # are bound here rather than constructed by `MemoryService` for the usual
        # reason, and for one more. The write path commits a store insert and its
        # sidecar row, and a sidecar written in a different transaction than the one
        # that recorded the memory's provenance is how a memory ends up in the vector
        # store with no scope — visible to a filter that defaults open, invisible to
        # one that defaults closed, and impossible to explain either way.
        self.memories = MemoryMetadataRepository(session)
        self.traces = ContextTraceRepository(session)
        self.promotions = PromotionRepository(session)
        self.entities = EntityRepository(session)
        self.intentions = IntentionRepository(session)
        self.procedures = ProcedureRepository(session)
        # M4. The config plane, and it is bound here for a reason the others share and
        # one they do not. Shared: everything an `apply` writes has to be one
        # transaction or the organization is half-built (§7). Specific: `lock_organization`
        # takes a *transaction-scoped* advisory lock, so it only serialises anything at
        # all if the lock and the writes are on this session.
        self.spec = SpecRepository(session)
        # M5. Both bound here for the reason every repository above is, and one more
        # that is specific to delegation: `start_run()` writes the child run, its spec,
        # its budget reservation, its outbox row *and* the `delegations` row that holds
        # the idempotency key, and those five are one transaction or the key has been
        # claimed by a child that does not exist. That is the same all-or-nothing this
        # class was built for, extended one table.
        self.delegations = DelegationRepository(session)
        self.departments = DepartmentRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Roll back on the way out unless the caller already committed. An
        # uncommitted session that is merely closed leaves the transaction to be
        # aborted by the server later, which holds locks for no reason.
        if exc_type is not None:
            await self.rollback()


class UnitOfWorkFactory:
    def __init__(self, settings: Settings | None = None) -> None:
        self._factory: async_sessionmaker[AsyncSession] = session_factory(settings)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[UnitOfWork]:
        """A read-oriented unit of work. Nothing is committed unless the caller
        asks.

        Closing the session — rather than rolling it back — is deliberate. Both
        release the connection and discard uncommitted writes, but `rollback()`
        also *expires* every loaded instance, so a caller that reads a row inside
        the block and touches an attribute after it would get a
        `DetachedInstanceError` on what looks like plain attribute access.
        """
        async with self._factory() as session, UnitOfWork(session) as uow:
            yield uow

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        """Open a unit of work that commits on clean exit and rolls back on any
        exception. Use this wherever "all of it or none of it" is the requirement."""
        async with self._factory() as session:
            uow = UnitOfWork(session)
            try:
                yield uow
                await uow.commit()
            except BaseException:
                await uow.rollback()
                raise
