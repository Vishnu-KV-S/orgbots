"""An `apply` in its own process, so it can be killed for real.

    python -m tests.chaos_applier <organization_id> <spec_path>

§12's hard exit criterion: *"`apply` is provably transactional: kill the process
mid-apply, assert no partial state."*

An in-process "crash" — an exception, a cancelled task — proves nothing here. Python
unwinds it, `async with` blocks run, and `UnitOfWorkFactory.transaction()` rolls back
politely. That is the *easy* case and `test_m4_apply` already covers it. What has to be
proved is the case where none of that happens: the process stops between two
instructions and the operating system reclaims it, mid-transaction, with rows already
written and uncommitted.

`RUNTIME_CHAOS_KILL_AFTER` names the phase to die in the middle of:

    entities   after phase 1 has written actors, roles and connections
    references after phase 2 has set placements, grants and triggers
    versions   after the actor versions are inserted and the pointers flipped

All three are inside the one transaction, so all three must leave nothing at all.
Postgres aborts the connection's transaction when the backend goes away, which is the
property under test — the applier does not have to do anything to make it true, but it
does have to not have committed early, and "did it commit early" is exactly what a
`SIGKILL` answers and a `raise` does not.

Killing from *inside* the process rather than from the parent is deliberate: it makes
the window deterministic. A parent that slept and then killed would be racing the child
and would sometimes kill it before it had written anything, which is a passing test that
proves nothing.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid

from runtime.domain.ids import OrganizationId
from runtime.persistence.engine import dispose_engines
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from runtime.spec.compile import compile_org
from runtime.spec.loader import load_path

KILL_AFTER_ENV = "RUNTIME_CHAOS_KILL_AFTER"
PHASES = ("entities", "references", "versions")


def _install_kill_hook(phase: str) -> None:
    """Patch the applier's repository so the process dies mid-transaction.

    Each phase is pinned to the *last* write that phase makes, so the kill lands with
    that phase complete and uncommitted — which is the state a partial apply would
    leave behind if the transaction were not doing its job.
    """
    from runtime.persistence.repositories.actors import ActorRepository
    from runtime.persistence.repositories.triggers import TriggerRepository

    def die() -> None:
        sys.stderr.write(f"chaos: killing after {phase}\n")
        sys.stderr.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    if phase == "entities":
        original_create = ActorRepository.create_actor
        seen: list[int] = []

        async def create_actor(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await original_create(self, *args, **kwargs)
            seen.append(1)
            if len(seen) >= 4:  # all four M1 actors written, nothing committed
                die()

        ActorRepository.create_actor = create_actor  # type: ignore[method-assign]

    elif phase == "references":
        original_upsert = TriggerRepository.upsert
        fired: list[int] = []

        async def upsert(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = await original_upsert(self, *args, **kwargs)
            fired.append(1)
            if len(fired) >= 4:  # every trigger installed
                die()
            return result

        TriggerRepository.upsert = upsert  # type: ignore[method-assign]

    elif phase == "versions":
        original_activate = ActorRepository.set_active_version

        async def set_active_version(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await original_activate(self, *args, **kwargs)
            die()

        ActorRepository.set_active_version = set_active_version  # type: ignore[method-assign]

    else:  # pragma: no cover - the parent validates the phase name
        raise SystemExit(f"unknown chaos phase {phase!r}; expected one of {PHASES}")


async def _main(organization_id: OrganizationId, path: str) -> int:
    from runtime.spec.apply import apply_org

    settings = Settings()
    uow = UnitOfWorkFactory(settings)
    try:
        org = compile_org(load_path(path))
        await apply_org(uow, org, organization_id=organization_id, applied_by="chaos")
        return 0
    finally:
        await dispose_engines()


if __name__ == "__main__":
    target = OrganizationId(uuid.UUID(sys.argv[1]))
    spec_path = sys.argv[2]
    os.environ.setdefault("RUNTIME_LOG_LEVEL", "WARNING")
    kill_after = os.environ.get(KILL_AFTER_ENV)
    if kill_after:
        _install_kill_hook(kill_after)
    sys.exit(asyncio.run(_main(target, spec_path)))
