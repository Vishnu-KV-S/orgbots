"""The organization layer.

Everything M1 adds that is about *work* rather than about *execution*: tasks and
their lifecycle, the inbox, sessions, evaluation, approvals, authority, the cron
vocabulary, and the reads of the metric views.

It sits below `runtime.gateway` in the layer contract, which is the one placement
decision worth explaining. The tool gateway has to consult authority and approvals
before an irreversible call — that is the M1 half of the pipeline order documented
in `gateway/tools.py` — and a gateway that imported *upwards* to reach them would
invert the whole stack. Putting the org layer underneath means the gateway asks
downwards like it asks the budget and the effect journal, and graphs above can use
the same services without a second copy.

Nothing here calls a model. Assignment, notification, closure, the deadline sweep
and the metric reads are all rules, which is what licenses §3's conclusion: if the
coordination ratio still comes out high, the hierarchy is the problem and not the
plumbing.

M2 adds two governance services at this level — the authority resolver and the kill
switch — for the same placement reason. Both are consulted by the gateway before an
external call, so both must be reachable downwards.
"""

from runtime.domain.authority import ActionAuthority, ResolvedAuthority
from runtime.domain.enums import AuthorityLevel
from runtime.org.approvals import ApprovalService
from runtime.org.authority import (
    AuthorityResolver,
    build_authority,
    check_delegation_subset,
    check_escalation_acyclicity,
)
from runtime.org.cron import CronExpr, parse_cron
from runtime.org.evaluation import EvaluationService, edit_distance
from runtime.org.goals import GoalService
from runtime.org.inbox import InboxService, dedupe_key
from runtime.org.killswitch import KillSwitchService, KillVerdict
from runtime.org.metrics import MetricsService, WeeklyMetrics
from runtime.org.sessions import SessionService, SessionView
from runtime.org.tasks import TaskService

__all__ = [
    "ActionAuthority",
    "ApprovalService",
    "AuthorityLevel",
    "AuthorityResolver",
    "CronExpr",
    "EvaluationService",
    "GoalService",
    "InboxService",
    "KillSwitchService",
    "KillVerdict",
    "MetricsService",
    "ResolvedAuthority",
    "SessionService",
    "SessionView",
    "TaskService",
    "WeeklyMetrics",
    "build_authority",
    "check_delegation_subset",
    "check_escalation_acyclicity",
    "dedupe_key",
    "edit_distance",
    "parse_cron",
]
