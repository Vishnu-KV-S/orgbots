"""The department: four actors, one goal, one project, four crons.

This is the hardcoding §12 says is the point. There is no YAML, no control plane
and no registry of departments — everything M1 runs is a value in this module, and
that is what makes the measurement fast enough to be honest.

**The four actors and why each one exists**

| actor | kind | classes | why |
|---|---|---|---|
| `marketing-head` | LLM | COORD, EVAL, SUMM | all the overhead, in one place |
| `research` | LLM | WORK | the expensive real work: search, fetch, synthesise |
| `content` | LLM | WORK | work that depends on another actor's output |
| `analytics` | DETERMINISTIC | *none* | the control: it cannot call a model |

`analytics` is the load-bearing one. It ships first (PR-19) and it is the only
actor whose output has no LLM anywhere in its provenance — which is why the go/no-go
numbers are read off it. It also proves the gateway refusal is real rather than
conventional: `max_llm_calls = 0` means `ModelGateway` rejects, and T24 makes it try.

**Ceilings live here, not in Settings.** They are frozen into the RunSpec at
admission, so an operator cannot widen a limit under a run that is already in
flight. The wall-clock numbers come from the M0 retro: `research` makes a search
call, several fetches and a model call at high effort, and M0's 300 s default would
have cut it off in a way that looks exactly like a lease problem while being
nothing of the kind.

**Model profiles.** Everything that judges or produces work runs on DeepSeek's pro
model; only SUMMARIZATION runs on flash. That is not the routing tiers §2 excludes —
the work-class → profile mapping is M0 machinery that already exists — but it is a
choice with a cost consequence, so it is stated rather than buried: compressing
history is the one call where a cheaper model cannot corrupt a deliverable, because
its output is never the artifact.

**One provider, and it is DeepSeek.** `anthropic` is not a name a profile can
resolve any more — `build_providers` does not register it — so there is no second
vendor to fall back to and no way for a profile to acquire one by typo. What remains
of it is the machinery underneath: `DeepSeekProvider` *is* the Messages client with a
different base URL, and the server-side search these profiles ask for is that
client's pause-and-continue loop driving an Anthropic server-tool type. The
consequence is a deployment one, and it is why `Settings.deepseek_enabled` defaults
on: with the flag off, a seeded department can make no model call at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from runtime.domain.enums import ActorKind, CatchupPolicy, WorkClass
from runtime.domain.ids import GoalId, OrganizationId, ProjectId, TriggerId
from runtime.domain.outputs import (
    COMPETITOR_REPORT_V1,
    CONTENT_DRAFT_V1,
    METRICS_REPORT_V1,
    WEEKLY_SUMMARY_V1,
)
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles

# --- actor names -------------------------------------------------------------------

HEAD = "marketing-head"
RESEARCH = "research"
CONTENT = "content"
ANALYTICS = "analytics"

ASSIGNEES = (RESEARCH, CONTENT, ANALYTICS)
"""Who the head may assign to. `WeeklyPlan` validation checks against this."""

ALL_ACTORS = (HEAD, *ASSIGNEES)

SCHEMA_FOR_ASSIGNEE = {
    RESEARCH: COMPETITOR_REPORT_V1,
    CONTENT: CONTENT_DRAFT_V1,
    ANALYTICS: METRICS_REPORT_V1,
}
"""Which output each assignee produces. The head may only pin these, which removes
a whole class of decomposition failure — asking `content` for a MetricsReport —
before it can become a rejected task."""

# --- models ------------------------------------------------------------------------

PRO = "deepseek-v4-pro"
FLASH = "deepseek-v4-flash"

_PRO_PROFILE = ModelProfile(
    provider="deepseek",
    model=PRO,
    max_output_tokens=16_000,
    temperature=0.0,
    # $0.66 / $1.98 per Mtok off-peak, in cents. Peak — 01:00-04:00 and 06:00-10:00
    # UTC, Mon-Fri — is exactly double, and the ledger has one rate per profile, so a
    # run inside those windows is under-counted. Same direction the token rounding
    # already errs in, and the wrong direction to be surprised by: if the weekly cron
    # moves into a peak window, these numbers move with it.
    input_cents_per_mtok=66,
    output_cents_per_mtok=198,
)

_FLASH_PROFILE = ModelProfile(
    provider="deepseek",
    model=FLASH,
    max_output_tokens=4_000,
    temperature=0.0,
    # $0.22 / $0.66 per Mtok off-peak, in cents.
    input_cents_per_mtok=22,
    output_cents_per_mtok=66,
)


MEMORY_PROFILE = _PRO_PROFILE.model_copy(update={"max_output_tokens": 4_000})
"""M3. The model that extracts and consolidates memories, and reviews promotions.

**Pro rather than flash, deliberately, for work that runs in a background worker.**
M3 §13 risk 6:
*"The extraction model determines memory quality permanently. A noisy fact written
today keeps being retrieved. Use a capable model here even though it runs off the hot
path — this is the wrong place to economize."* Every other cheap-model decision in this
codebase is reversible by re-running the call; this one is not, because the bad fact is
already in the store being retrieved.

The counterweight is risk 4 — *"consolidation cost is invisible until you look"* — and
it is why this is `work_class=MEMORY` and why that class is on the dashboard from day
one rather than folded into `SUMMARIZATION`. If the number comes out badly, the response
is a cheaper model *here*, chosen against a measurement, which is a different thing from
having economised before there was one.

`max_output_tokens` is small because extraction emits a short JSON object, not prose. It
is the one dial that can be tightened without touching what the model is capable of.

Not in any `ActorSpec`. Memory calls are made by the worker after a run has finished, not
by an actor during one, so adding this to a spec would change every `spec_hash` in the
department to describe a call the run cannot make.
"""


def _pro(*, searching: bool) -> ModelProfile:
    """The pro profile, with thinking off exactly where the search is on.

    Not a tuning preference. A provider-side search pauses the turn, and resuming it
    means handing the vendor's own assistant blocks back — where a thinking block the
    endpoint wants signed is refused as a malformed request. `AnthropicProvider`
    treats that refusal as non-fatal and keeps the partial answer, so the failure is
    not an error: it is a report written without the evidence the search just paid
    for. Nothing in a competitor report needs the thinking budget WORK-class effort
    already buys, so this is the cheap side of that trade.
    """
    return _PRO_PROFILE.model_copy(update={"web_search": searching, "thinking": not searching})


def _profiles(*classes: WorkClass, web_search: bool = False) -> ModelProfiles:
    """Give an actor a profile for exactly the classes it uses, and no others.

    An actor with no profile for a class cannot make a call in that class —
    `for_work_class` raises `SpecError` at the gateway. So this is not
    configuration, it is a per-actor allow-list over the M1 vocabulary, and it is
    half of what makes T25 hold by construction rather than by inspection.

    `web_search` asks the provider to search server-side, and it lands on the WORK
    profile only: a search during summarisation is spend with nothing to gain. It is
    the *request*; the actor's `web.search@1` grant is the permission, and both have
    to be there — which is why passing it for an actor without the grant would be a
    no-op rather than a widening.
    """
    return ModelProfiles(
        profiles={
            wc: (
                _FLASH_PROFILE
                if wc is WorkClass.SUMMARIZATION
                else _pro(searching=web_search and wc is WorkClass.WORK)
            )
            for wc in classes
        }
    )


# --- the four specs ----------------------------------------------------------------

HEAD_SPEC = ActorSpec(
    name=HEAD,
    kind=ActorKind.LLM_AGENT,
    graph_ref="marketing_head@1",
    # The head is the only actor holding the irreversible tool, because it is the
    # only one that owns the approval gate. An assignee that could publish would
    # make the gate advisory.
    allowed_tools=frozenset({"publish.external@1"}),
    ceilings=Ceilings(
        max_llm_calls=8,
        max_tool_calls=4,
        max_wall_clock_s=600.0,
        max_cost_cents=400,
    ),
    model_profiles=_profiles(WorkClass.COORDINATION, WorkClass.EVALUATION, WorkClass.SUMMARIZATION),
)

RESEARCH_SPEC = ActorSpec(
    name=RESEARCH,
    kind=ActorKind.LLM_AGENT,
    graph_ref="research@1",
    allowed_tools=frozenset({"web.search@1", "web.fetch@1"}),
    ceilings=Ceilings(
        max_llm_calls=6,
        max_tool_calls=24,
        # From the M0 retro: a search, several fetches and one high-effort call.
        max_wall_clock_s=900.0,
        max_cost_cents=600,
    ),
    # The only actor that asks for it, because it is the only one holding
    # `web.search@1`. This is now a live provider-side search rather than an inert
    # flag: DeepSeek has the capability where Anthropic does not, so it is offered
    # whenever `RUNTIME_DEEPSEEK_WEB_SEARCH` is on — and only ever to this actor.
    model_profiles=_profiles(WorkClass.WORK, WorkClass.SUMMARIZATION, web_search=True),
)

CONTENT_SPEC = ActorSpec(
    name=CONTENT,
    kind=ActorKind.LLM_AGENT,
    graph_ref="content@1",
    allowed_tools=frozenset({"web.fetch@1"}),
    ceilings=Ceilings(
        max_llm_calls=6,
        max_tool_calls=8,
        max_wall_clock_s=600.0,
        max_cost_cents=500,
    ),
    model_profiles=_profiles(WorkClass.WORK, WorkClass.SUMMARIZATION),
)

ANALYTICS_SPEC = ActorSpec(
    name=ANALYTICS,
    kind=ActorKind.DETERMINISTIC_WORKER,
    handler_ref="analytics@1",
    allowed_tools=frozenset(),
    ceilings=Ceilings(
        # The whole point. Not "we chose not to call a model" — the gateway refuses.
        max_llm_calls=0,
        max_tool_calls=0,
        max_wall_clock_s=120.0,
        max_cost_cents=0,
    ),
    # No profiles at all, so even if the ceiling were raised there would be nothing
    # to resolve. Two independent refusals for the one actor that must not spend.
    model_profiles=ModelProfiles(),
)

DEPARTMENT_SPECS = (HEAD_SPEC, RESEARCH_SPEC, CONTENT_SPEC, ANALYTICS_SPEC)


# --- the goal and project ----------------------------------------------------------

GOAL_NAME = "q3-category-position"
GOAL_STATEMENT = (
    "Establish a defensible position in the agent-infrastructure category: know who "
    "else is in it, what they claim, and where the gaps are; publish one substantive "
    "piece a week that speaks to a gap; and report weekly on whether any of it is "
    "working."
)
PROJECT_NAME = "weekly-marketing-loop"
PROJECT_DESCRIPTION = "The Monday-to-Friday cycle: plan, research, draft, gate, measure, summarise."


def goal_id_for(organization_id: OrganizationId) -> GoalId:
    return GoalId(uuid.uuid5(uuid.NAMESPACE_URL, f"goal:{organization_id}:{GOAL_NAME}"))


def project_id_for(organization_id: OrganizationId) -> ProjectId:
    return ProjectId(uuid.uuid5(uuid.NAMESPACE_URL, f"project:{organization_id}:{PROJECT_NAME}"))


# --- the schedule ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    key: str
    actor_name: str
    cron: str
    mode: str
    catchup: CatchupPolicy = CatchupPolicy.SKIP

    def input(self) -> dict[str, object]:
        return {"mode": self.mode, "trigger": self.key}


TRIGGERS = (
    TriggerSpec("weekly-plan", HEAD, "0 8 * * 1", "weekly_plan"),
    # §3's loop. §2's scope line names two crons — the plan and the summary — and
    # these are the other two the loop it describes actually needs. The scheduler
    # machinery is identical for two or four; what four buys is that the Thursday
    # gate and the Friday metrics run on the same dedupe-and-catch-up path as
    # everything else, rather than being hand-invoked steps nobody measures.
    TriggerSpec("publish-gate", HEAD, "0 16 * * 4", "publish_gate"),
    TriggerSpec("weekly-metrics", ANALYTICS, "0 16 * * 5", "weekly_metrics"),
    TriggerSpec("weekly-summary", HEAD, "0 17 * * 5", "weekly_summary"),
)

MODES = frozenset(t.mode for t in TRIGGERS) | {"evaluate", "work"}
"""Every entry point an actor graph dispatches on. `evaluate` and `work` arrive by
inbox message rather than by cron."""


def trigger_id_for(organization_id: OrganizationId, key: str) -> TriggerId:
    return TriggerId(uuid.uuid5(uuid.NAMESPACE_URL, f"trigger:{organization_id}:{key}"))


def week_of(when: dt.datetime | None = None) -> dt.date:
    """The Monday of the week containing `when`, UTC.

    The loop's unit. Used for session keys, plan correlation and the metric views'
    `week_start`, so all three line up without anyone converting between them.
    """
    moment = (when or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    return (moment - dt.timedelta(days=moment.weekday())).date()


def correlation_for_week(organization_id: OrganizationId, week: dt.date) -> uuid.UUID:
    """One correlation id per week, derived.

    Everything the Monday plan sets in motion — tasks, messages, evaluations,
    the Friday summary — carries it, so "show me everything that happened because
    of week N" is one indexed query rather than a reconstruction.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"week:{organization_id}:{week.isoformat()}")


# --- M3: memory scopes -------------------------------------------------------------

DEPARTMENT_NAME = "marketing"


def company_scope_id(organization_id: OrganizationId) -> uuid.UUID:
    """The `COMPANY` scope's id. Derived, so it needs no row and no seeding step.

    `uuid5` over the organization, the same trick `goal_id_for` uses. A scope id that
    had to be looked up would make the retrieval path depend on a table read before it
    can build its filter, and a filter that cannot be built is a retrieval that either
    fails or — much worse — falls back to something wider.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memscope:company:{organization_id}")


def department_scope_id(
    organization_id: OrganizationId, department: str | None = None
) -> uuid.UUID:
    """The `DEPARTMENT` scope's id, derived from the organization and the name.

    From the *name*, which looks like it contradicts 025's "never the name, because a
    department renamed in week nine would orphan every memory". It does not: the id is
    derived once and stored on every row, so a later rename produces a *new* scope
    whose emptiness is immediately visible, rather than silently re-pointing existing
    rows at a scope nobody reviewed. Renaming a department is a migration, and M3's
    scope is one department (§2's "Out: ... a second department") so it is not one that
    has to be written yet.
    """
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"memscope:department:{organization_id}:{department or DEPARTMENT_NAME}"
    )


SCHEMA_TO_ASSIGNEE = {ref: name for name, ref in SCHEMA_FOR_ASSIGNEE.items()}
TASK_SCHEMAS = frozenset(SCHEMA_FOR_ASSIGNEE.values()) | {WEEKLY_SUMMARY_V1}
