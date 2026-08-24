"""Typed identifiers.

These are `NewType` over `UUID`, not subclasses: they cost nothing at runtime and
mypy still refuses to pass a `RunId` where an `ActorId` is expected. Every ID that
appears in a log line or a foreign key has a name here.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID, uuid4

OrganizationId = NewType("OrganizationId", UUID)
ActorId = NewType("ActorId", UUID)
ActorVersionId = NewType("ActorVersionId", int)
RunId = NewType("RunId", UUID)
SessionId = NewType("SessionId", UUID)
WorkerId = NewType("WorkerId", UUID)
ArtifactId = NewType("ArtifactId", UUID)
ArtifactVersionId = NewType("ArtifactVersionId", int)
BudgetPoolId = NewType("BudgetPoolId", UUID)
ReservationId = NewType("ReservationId", UUID)
EffectId = NewType("EffectId", UUID)

# --- M1: the organization ----------------------------------------------------------

GoalId = NewType("GoalId", UUID)
ProjectId = NewType("ProjectId", UUID)
TaskId = NewType("TaskId", UUID)
EvaluationId = NewType("EvaluationId", UUID)
MessageId = NewType("MessageId", UUID)
TriggerId = NewType("TriggerId", UUID)
ApprovalId = NewType("ApprovalId", UUID)

# --- M3: memory --------------------------------------------------------------------

MemoryId = NewType("MemoryId", str)
"""**A string, not a UUID, and that is not an oversight.**

`memory_metadata.memory_id` is the *store's* id, and the store is a port with more
than one adapter. Mem0 mints UUID-shaped strings; a different vector store need not.
Typing this as a UUID would bake one adapter's choice into every foreign key that
references it, which is precisely the coupling the port exists to avoid.
"""

EntityId = NewType("EntityId", UUID)
PromotionId = NewType("PromotionId", UUID)
IntentionId = NewType("IntentionId", UUID)
ProcedureCandidateId = NewType("ProcedureCandidateId", UUID)
ContextTraceId = NewType("ContextTraceId", UUID)

CorrelationId = NewType("CorrelationId", UUID)
"""Ties every message, task and run belonging to one unit of work together.

Set once at the head of a chain — a cron fire, a task assignment — and copied,
never regenerated, by everything downstream. It is what makes "show me everything
that happened because of Monday's plan" a single indexed query.
"""

Fence = NewType("Fence", int)
"""Monotonic per-run lease generation. Advances on every claim; a run whose fence
has moved past the one a worker holds must not be allowed to commit anything."""


def new_organization_id() -> OrganizationId:
    return OrganizationId(uuid4())


def new_actor_id() -> ActorId:
    return ActorId(uuid4())


def new_run_id() -> RunId:
    return RunId(uuid4())


def new_session_id() -> SessionId:
    return SessionId(uuid4())


def new_worker_id() -> WorkerId:
    return WorkerId(uuid4())


def new_artifact_id() -> ArtifactId:
    return ArtifactId(uuid4())


def new_budget_pool_id() -> BudgetPoolId:
    return BudgetPoolId(uuid4())


def new_reservation_id() -> ReservationId:
    return ReservationId(uuid4())


def new_effect_id() -> EffectId:
    return EffectId(uuid4())


def new_goal_id() -> GoalId:
    return GoalId(uuid4())


def new_project_id() -> ProjectId:
    return ProjectId(uuid4())


def new_task_id() -> TaskId:
    return TaskId(uuid4())


def new_evaluation_id() -> EvaluationId:
    return EvaluationId(uuid4())


def new_message_id() -> MessageId:
    return MessageId(uuid4())


def new_trigger_id() -> TriggerId:
    return TriggerId(uuid4())


def new_approval_id() -> ApprovalId:
    return ApprovalId(uuid4())


def new_correlation_id() -> CorrelationId:
    return CorrelationId(uuid4())


def new_memory_id() -> MemoryId:
    return MemoryId(str(uuid4()))


def new_entity_id() -> EntityId:
    return EntityId(uuid4())


def new_promotion_id() -> PromotionId:
    return PromotionId(uuid4())


def new_intention_id() -> IntentionId:
    return IntentionId(uuid4())


def new_procedure_candidate_id() -> ProcedureCandidateId:
    return ProcedureCandidateId(uuid4())


def new_context_trace_id() -> ContextTraceId:
    return ContextTraceId(uuid4())
