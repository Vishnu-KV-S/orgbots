"""Capability entailment and blast-radius defaults.

Two checks run at tool registration, both of which fail the process rather than
the request. A tool whose declared recovery policy is a lie is not a runtime
error; it is a deployment that should not have started.

**Entailment.** A recovery policy is a promise about what the runtime can do after
a crash, and each promise needs a specific capability to keep:

| policy            | requires                                            |
|-------------------|-----------------------------------------------------|
| `replay_safe`     | `mutates_external_state` is False                    |
| `idempotency_key` | provider accepts a key, and a field to put it in     |
| `probe`           | a searchable marker, a field for it, and a search fn |
| `manual`          | nothing — it is the admission that we cannot recover |

The failure this prevents is subtle and expensive: a tool declares `probe`, has no
marker, and every replay silently degrades to re-execution. Nothing errors. The
duplicate charge shows up in a customer's statement.

**Blast radius drives policy, not just recovery.** `max_blast_radius` is an input
to the policy resolver at registration:

- `read` → auto, no approval, retries allowed
- `reversible` → auto under a declared threshold, standard audit
- `irreversible` → approval required by default, `retries=0`, elevated audit
  severity, allow-list only

A tool may tighten its class's defaults. It may never loosen them.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.enums import AuditSeverity, BlastRadius, RecoveryPolicy
from runtime.domain.errors import IncoherentEffectPolicy


@dataclass(frozen=True, slots=True)
class PolicyDefaults:
    max_retries: int
    requires_approval: bool
    audit_severity: AuditSeverity


BLAST_RADIUS_DEFAULTS: dict[BlastRadius, PolicyDefaults] = {
    BlastRadius.READ: PolicyDefaults(
        max_retries=3, requires_approval=False, audit_severity=AuditSeverity.LOW
    ),
    BlastRadius.REVERSIBLE: PolicyDefaults(
        max_retries=2, requires_approval=False, audit_severity=AuditSeverity.NORMAL
    ),
    BlastRadius.IRREVERSIBLE: PolicyDefaults(
        max_retries=0, requires_approval=True, audit_severity=AuditSeverity.HIGH
    ),
}

_SEVERITY_ORDER = {AuditSeverity.LOW: 0, AuditSeverity.NORMAL: 1, AuditSeverity.HIGH: 2}


def check_entailment(
    policy: RecoveryPolicy,
    *,
    tool_name: str,
    mutates_external_state: bool,
    accepts_idempotency_key: bool,
    idempotency_key_field: str | None,
    searchable_marker: bool,
    marker_field: str | None,
    marker_search_fn: str | None,
) -> None:
    """Raise `IncoherentEffectPolicy` if the capabilities cannot support the policy."""
    if policy is RecoveryPolicy.REPLAY_SAFE:
        if mutates_external_state:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares replay_safe but also mutates_external_state. "
                "A replay would perform the mutation again."
            )
        return

    if not mutates_external_state:
        raise IncoherentEffectPolicy(
            f"{tool_name}: declares {policy.value} but does not mutate external state. "
            "Use replay_safe; a recovery policy that never applies hides the fact "
            "that nothing is being recovered."
        )

    if policy is RecoveryPolicy.IDEMPOTENCY_KEY:
        if not accepts_idempotency_key:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares idempotency_key recovery but the provider does "
                "not accept an idempotency key."
            )
        if not idempotency_key_field:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares idempotency_key recovery without naming the "
                "field to send it in."
            )
        return

    if policy is RecoveryPolicy.PROBE:
        if not searchable_marker:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares probe recovery without a searchable marker. "
                "Every replay would silently degrade to re-execution."
            )
        if not marker_field:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares probe recovery without naming the marker field."
            )
        if not marker_search_fn:
            raise IncoherentEffectPolicy(
                f"{tool_name}: declares probe recovery without a marker_search_fn. "
                "A marker nothing can search for is not a marker."
            )
        return

    # MANUAL entails nothing; it is the honest admission that recovery is impossible.


def resolve_policy(
    blast_radius: BlastRadius,
    *,
    tool_name: str,
    max_retries: int | None,
    requires_approval: bool | None,
    audit_severity: AuditSeverity | None,
    allow_listed: bool = False,
) -> PolicyDefaults:
    """Apply the blast-radius defaults; a tool may tighten them, never loosen.

    `allow_listed` is the one escape from mandatory approval on an irreversible
    tool, and it is a deliberate, named, reviewable act rather than a field the
    tool sets about itself.
    """
    defaults = BLAST_RADIUS_DEFAULTS[blast_radius]

    resolved_retries = defaults.max_retries if max_retries is None else max_retries
    if resolved_retries > defaults.max_retries:
        raise IncoherentEffectPolicy(
            f"{tool_name}: blast radius {blast_radius.value} allows at most "
            f"{defaults.max_retries} retries, tool asked for {resolved_retries}"
        )

    resolved_approval = (
        defaults.requires_approval if requires_approval is None else requires_approval
    )
    if defaults.requires_approval and not resolved_approval and not allow_listed:
        raise IncoherentEffectPolicy(
            f"{tool_name}: blast radius {blast_radius.value} requires approval; "
            "a tool cannot waive it for itself — add it to the allow-list instead"
        )

    resolved_severity = defaults.audit_severity if audit_severity is None else audit_severity
    if _SEVERITY_ORDER[resolved_severity] < _SEVERITY_ORDER[defaults.audit_severity]:
        raise IncoherentEffectPolicy(
            f"{tool_name}: blast radius {blast_radius.value} requires audit severity "
            f"at least {defaults.audit_severity.value}, tool asked for "
            f"{resolved_severity.value}"
        )

    return PolicyDefaults(
        max_retries=resolved_retries,
        requires_approval=resolved_approval,
        audit_severity=resolved_severity,
    )
