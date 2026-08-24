"""The effect journal.

This is the mechanism the whole milestone exists to prove. Everything else in M0
is scaffolding around one claim: *a tool that mutates external state executes
exactly once per logical call, no matter where the process dies.*

The claim rests on two things.

**A logical call identity that survives replay.** `logical_call_id` is a pure
function of five values that a replayed node reproduces exactly. It cannot contain
a timestamp, a `uuid4`, a retry counter, or anything else that differs between the
attempt that died and the attempt that resumes — if it did, the replay would mint
a fresh identity and fire the effect a second time. T6 tests this as a property,
including across process restarts, because a `hash()`-based implementation passes
every single-process test and fails in production.

**An INTENT row that commits before the effect fires, on its own connection.** The
window between "we are about to do this" and "we did this" is where crashes live.
Writing INTENT first turns an invisible window into a visible one: after any crash,
an INTENT row means "this effect may or may not have happened", and the recovery
policy — which is a property of the tool, declared and validated at registration —
decides what to do about it.

The separate connection is not optional. If the INTENT row were written in the
caller's transaction and the caller rolled back, the record of an effect that had
already fired would vanish with it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from runtime.domain.context import RunContext
from runtime.domain.enums import BlastRadius, EffectStatus, RecoveryPolicy
from runtime.domain.ids import EffectId, new_effect_id
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.effects import EffectRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("effects.journal")


def logical_call_id(
    ctx: RunContext, node: str, ordinal: int, args_hash: str, checkpoint_ns: str = ""
) -> str:
    """H(run_id, checkpoint_ns, node, ordinal, args_hash).

    MUST be a pure function of these five values. Any use of `uuid4`, `time`, or
    `random` here is a correctness bug — see test T6.

    Note what is deliberately *absent*: the fence. Two attempts at the same logical
    call are the same logical call precisely because they happen under different
    fences; including it would give the replay a different identity and defeat the
    entire mechanism.
    """
    material = "\x1f".join(
        [
            str(ctx.run_id),
            checkpoint_ns,
            node,
            str(ordinal),
            args_hash,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EffectMeta:
    tool_name: str
    tool_version: int
    recovery_policy: RecoveryPolicy
    blast_radius: BlastRadius
    node: str
    checkpoint_ns: str
    ordinal: int
    args_hash: str
    idempotency_key: str | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class EffectLookup:
    """What `begin()` found.

    `is_replay` is the only field callers branch on for correctness: it means an
    earlier attempt at this exact logical call reached the journal, so the tool may
    already have fired.
    """

    effect_id: EffectId
    status: EffectStatus
    is_replay: bool
    row: EffectRow

    @property
    def already_committed(self) -> bool:
        return self.status is EffectStatus.COMMITTED


class EffectJournalProtocol(Protocol):
    async def begin(self, key: str, meta: EffectMeta, ctx: RunContext) -> EffectLookup: ...
    async def commit(self, key: str, result_ref: UUID | None, provider_ref: str | None) -> None: ...
    async def fail(self, key: str, error: str) -> None: ...


class EffectJournal:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def begin(self, key: str, meta: EffectMeta, ctx: RunContext) -> EffectLookup:
        """Record the intent to perform an effect, and report what was already
        there.

        This commits on its own connection, immediately, before the caller does
        anything external. The commit is the point: an uncommitted intent is
        invisible to the recovery path, which is another process entirely.
        """
        async with self._uow.transaction() as uow:
            row, created = await uow.effects.upsert_intent(
                {
                    "id": new_effect_id(),
                    "logical_call_id": key,
                    "organization_id": ctx.organization_id,
                    "run_id": ctx.run_id,
                    "root_run_id": ctx.root_run_id,
                    "fence": int(ctx.fence),
                    "node": meta.node,
                    "checkpoint_ns": meta.checkpoint_ns,
                    "ordinal": meta.ordinal,
                    "args_hash": meta.args_hash,
                    "tool_name": meta.tool_name,
                    "tool_version": meta.tool_version,
                    "recovery_policy": meta.recovery_policy.value,
                    "blast_radius": meta.blast_radius.value,
                    "idempotency_key": meta.idempotency_key,
                    "marker": meta.marker,
                }
            )
        if not created:
            log.info(
                "effect.replay_detected",
                logical_call_id=key,
                status=row.status.value,
                attempts=row.attempts,
                tool=meta.tool_name,
                **ctx.log_fields(),
            )
        return EffectLookup(
            effect_id=EffectId(row.id),
            status=row.status,
            is_replay=not created,
            row=row,
        )

    async def commit(
        self,
        key: str,
        result_ref: UUID | None = None,
        provider_ref: str | None = None,
        *,
        result_inline: dict[str, object] | None = None,
        marker: str | None = None,
    ) -> None:
        """Record that the effect definitively happened, with its result."""
        async with self._uow.transaction() as uow:
            await uow.effects.commit_effect(
                key,
                result_ref=result_ref,
                result_inline=result_inline,
                provider_ref=provider_ref,
                marker=marker,
            )

    async def fail(self, key: str, error: str) -> None:
        """Record that the effect definitively did NOT happen.

        Only call this when that is actually known. A timeout is not a failure —
        it is an unknown, and marking an unknown as FAILED tells a later replay it
        is safe to fire again. That is how you get two payments.
        """
        async with self._uow.transaction() as uow:
            await uow.effects.settle(key, EffectStatus.FAILED, error)

    async def orphan(self, key: str, error: str) -> None:
        """Record that we cannot determine whether the effect happened.

        This is a dead end on purpose. A human resolves it. The alternative —
        guessing — is what the journal exists to avoid.
        """
        async with self._uow.transaction() as uow:
            await uow.effects.settle(key, EffectStatus.ORPHANED, error)
        log.error("effect.orphaned", logical_call_id=key, error=error)

    async def get(self, key: str) -> EffectRow | None:
        async with self._uow() as uow:
            return await uow.effects.get(key)
