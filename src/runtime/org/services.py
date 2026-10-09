"""One bundle of org services, constructed once per worker.

A graph node needs four or five of these and constructing them per node would mean
each one holding its own view of the same unit-of-work factory. Worse, it would
make a node's dependencies invisible at the point a test wants to substitute one.

So they are built once, handed to the executor, and reach a node through the
LangGraph config alongside the gateways — the same mechanism, for the same reason:
`runtime.graphs` stays free of the imports the `gateway-only` contract forbids, and
a graph can be exercised against fakes without patching anything global.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.artifacts.store import ArtifactStore
from runtime.org.approvals import ApprovalService
from runtime.org.bots import BotService
from runtime.org.connectors import ConnectorService
from runtime.org.evaluation import EvaluationService
from runtime.org.files import TeamDrive
from runtime.org.goals import GoalService
from runtime.org.groups import GroupService
from runtime.org.inbox import InboxService
from runtime.org.metrics import MetricsService
from runtime.org.routines import RoutineService
from runtime.org.sessions import SessionService
from runtime.org.skills import SkillService
from runtime.org.tasks import TaskService
from runtime.persistence.uow import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class OrgServices:
    tasks: TaskService
    goals: GoalService
    inbox: InboxService
    sessions: SessionService
    evaluation: EvaluationService
    approvals: ApprovalService
    metrics: MetricsService
    bots: BotService
    files: TeamDrive
    routines: RoutineService
    skills: SkillService
    groups: GroupService
    connectors: ConnectorService


def build_org_services(
    uow_factory: UnitOfWorkFactory,
    artifacts: ArtifactStore | None = None,
    *,
    bot_chunks: int = 1,
) -> OrgServices:
    """Wire the services so they share one inbox and one task service.

    Sharing matters: `EvaluationService` sends messages through the *same*
    `InboxService` instance that `TaskService` uses, so a test that swaps the inbox
    for a recording fake sees every message the whole evaluation path produces,
    not the subset that happened to go through the object it replaced.
    """
    inbox = InboxService(uow_factory)
    tasks = TaskService(uow_factory, inbox=inbox, artifacts=artifacts)
    return OrgServices(
        tasks=tasks,
        goals=GoalService(uow_factory),
        inbox=inbox,
        sessions=SessionService(uow_factory),
        evaluation=EvaluationService(uow_factory, tasks=tasks, inbox=inbox),
        approvals=ApprovalService(uow_factory),
        metrics=MetricsService(uow_factory),
        bots=BotService(uow_factory, inbox=inbox, max_chunks=bot_chunks),
        files=TeamDrive(uow_factory),
        routines=RoutineService(uow_factory),
        skills=SkillService(uow_factory),
        groups=GroupService(uow_factory),
        connectors=ConnectorService(uow_factory),
    )
