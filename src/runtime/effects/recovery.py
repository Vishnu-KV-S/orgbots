"""Recovery decisions.

Given what the journal found, decide whether to execute the tool. This is a pure
decision function plus one I/O call (the probe), kept apart from the gateway so
the interesting cases can be tested without a gateway, a run, or a lease.

The decision table:

| journal state        | replay_safe | idempotency_key | probe            | manual  |
|----------------------|-------------|-----------------|------------------|---------|
| nothing (first call) | EXECUTE     | EXECUTE         | EXECUTE          | EXECUTE |
| COMMITTED            | RETURN      | RETURN          | RETURN           | RETURN  |
| INTENT (crash)       | EXECUTE     | EXECUTE w/ key  | probe → RETURN   | ORPHAN  |
|                      |             |                 |    or EXECUTE    |         |
| FAILED               | EXECUTE     | EXECUTE w/ key  | EXECUTE          | ORPHAN  |
| ORPHANED             | refuse      | refuse          | refuse           | refuse  |

The FAILED row is only sound because `journal.fail()` is reserved for effects
*known* not to have happened. A timeout must never be recorded as FAILED.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from runtime.domain.enums import EffectStatus, RecoveryPolicy
from runtime.domain.errors import EffectOrphaned
from runtime.effects.journal import EffectLookup
from runtime.effects.probes import get_probe
from runtime.observability.logging import get_logger

log = get_logger("effects.recovery")


class Action(StrEnum):
    EXECUTE = "execute"
    RETURN_RECORDED = "return_recorded"
    ORPHAN = "orphan"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    probe_result: dict[str, object] | None = None


async def decide(
    lookup: EffectLookup,
    *,
    policy: RecoveryPolicy,
    marker: str | None,
    marker_search_fn: str | None,
    tool_name: str,
) -> Decision:
    if lookup.status is EffectStatus.ORPHANED:
        raise EffectOrphaned(
            f"{tool_name}: effect {lookup.row.logical_call_id} is ORPHANED and needs a "
            "human decision; refusing to guess"
        )

    if lookup.already_committed:
        return Decision(Action.RETURN_RECORDED, "already committed")

    if not lookup.is_replay:
        return Decision(Action.EXECUTE, "first attempt")

    if lookup.status is EffectStatus.FAILED:
        # Known not to have happened. Safe to run again under any policy that can
        # run at all.
        if policy is RecoveryPolicy.MANUAL:
            return Decision(Action.ORPHAN, "manual policy cannot retry after failure")
        return Decision(Action.EXECUTE, "previous attempt definitively failed")

    # Status is INTENT: the effect may or may not have fired.
    match policy:
        case RecoveryPolicy.REPLAY_SAFE:
            return Decision(Action.EXECUTE, "replay_safe: re-execution is harmless")

        case RecoveryPolicy.IDEMPOTENCY_KEY:
            return Decision(Action.EXECUTE, "idempotency_key: provider dedupes on the same key")

        case RecoveryPolicy.PROBE:
            if not marker or not marker_search_fn:
                # Registration should have made this impossible; if it happens, the
                # honest answer is that we do not know.
                return Decision(Action.ORPHAN, "probe policy with no usable marker")
            found = await get_probe(marker_search_fn)(marker)
            if found is not None:
                log.info("effect.probe_hit", tool=tool_name, marker=marker)
                return Decision(Action.RETURN_RECORDED, "probe found the marker", found)
            log.info("effect.probe_miss", tool=tool_name, marker=marker)
            return Decision(Action.EXECUTE, "probe found nothing; effect never landed")

        case RecoveryPolicy.MANUAL:
            return Decision(
                Action.ORPHAN, "manual policy: cannot determine whether the effect landed"
            )

    raise AssertionError(f"unhandled recovery policy {policy}")  # pragma: no cover
