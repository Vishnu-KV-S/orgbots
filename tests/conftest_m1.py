"""Fixtures for the M1 department.

Two things live here and the second is the interesting one.

`sample_*()` build valid instances of each pinned output schema. They are used as
canned model responses and as the starting point for tests that need an *invalid*
output — mutate one field and the cross-field validator fires, which is a much
better test than hand-writing broken JSON that fails for the wrong reason.

`ScriptedProvider` is a `Provider` that answers by call site from a script, and
records every request it was given. It is what makes T25 possible: the graphs run
end to end, and afterwards the recording says which work class every call declared.
It is deliberately *not* a mock of `ModelGateway` — the gateway is where I12 is
enforced, so a test that stubbed it out would be asserting on the stub.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from runtime.artifacts.store import ArtifactStore
from runtime.domain.enums import TrustLevel
from runtime.domain.ids import CorrelationId, OrganizationId
from runtime.domain.outputs import word_count
from runtime.domain.specs import ModelProfile
from runtime.events.relay import OutboxRelay
from runtime.events.stream import RedisStreams
from runtime.gateway.models import ModelRequest, ModelResponse
from runtime.graphs.checkpointer import checkpointer
from runtime.org.department import DEPARTMENT_SPECS, correlation_for_week, week_of
from runtime.org.services import OrgServices, build_org_services
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.department_boot import Department, seed_department
from runtime.runtime.dispatcher import Dispatcher
from runtime.runtime.run_service import RunService
from runtime.runtime.scheduler import Scheduler
from runtime.settings import Settings
from runtime.worker.worker import Worker

TODAY = dt.date(2026, 8, 17)  # a Monday


# --- valid instances of every pinned schema ----------------------------------------


def sample_source(i: int = 0) -> dict[str, Any]:
    return {
        "url": f"https://example.com/source-{i}",
        "title": f"Source {i}: what the competitor says about itself",
        "quote": (
            "We build durable infrastructure for long-running agents, priced per "
            f"seat rather than per token, and we do not publish list pricing ({i})."
        ),
        "retrieved_at": TODAY.isoformat(),
    }


def sample_competitor_report(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject": "agent infrastructure",
        "as_of": TODAY.isoformat(),
        "summary": (
            "Four vendors compete on durability guarantees rather than on model "
            "quality. Two publish pricing; the other two gate it behind a sales "
            "conversation, which is itself a positioning choice worth naming."
        ),
        "competitors": [
            {
                "name": "Durable Systems",
                "url": "https://example.com/durable",
                "positioning": (
                    "Sells exactly-once execution as the product rather than as a "
                    "feature, and leads every page with the crash-recovery story."
                ),
                "pricing_note": "Per seat, published.",
                "source_indices": [0],
            },
            {
                "name": "Orchestra",
                "url": "https://example.com/orchestra",
                "positioning": (
                    "Positions against workflow engines rather than against agent "
                    "frameworks, and does not mention reliability on the home page."
                ),
                "pricing_note": None,
                "source_indices": [1, 2],
            },
        ],
        "themes": [
            {
                "statement": (
                    "Nobody in this category talks about what one accepted outcome "
                    "costs; every vendor prices the input rather than the result."
                ),
                "competitors": ["Durable Systems", "Orchestra"],
                "confidence": "medium",
            },
            {
                "statement": (
                    "Reliability language is present but unquantified: three of the "
                    "four say 'durable' and none of them define it."
                ),
                "competitors": ["Durable Systems"],
                "confidence": "high",
            },
        ],
        "recommendations": [
            {
                "action": (
                    "Publish the cost of one accepted outcome, with the method, "
                    "before anyone else in the category does."
                ),
                "rationale": (
                    "No competitor prices the result rather than the input, so this "
                    "is an unclaimed position and it is one we can substantiate."
                ),
                "effort": "M",
                "source_indices": [0, 1],
            }
        ],
        "sources": [sample_source(i) for i in range(3)],
        "gaps": ["Could not establish Orchestra's pricing; it is not published."],
    }
    payload.update(overrides)
    return payload


BODY = (
    "## The number nobody publishes\n\n"
    + (
        "Every vendor in this category prices the input: tokens, seats, runs. "
        "None of them will tell you what one accepted piece of work costs, and "
        "that is not an oversight. It is the number that is hard to defend. "
    )
    * 12
)


def sample_content_draft(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "The number nobody in agent infrastructure will publish",
        "channel": "blog",
        "audience": "Engineering leads evaluating agent platforms this quarter",
        "thesis": (
            "Pricing the input rather than the outcome is a choice that hides the "
            "only number a buyer actually needs, and we should publish ours."
        ),
        "sections": [
            {
                "heading": "What everyone prices",
                "key_point": (
                    "Tokens, seats and runs are all inputs, and all three are "
                    "uncorrelated with whether the work was any good."
                ),
            },
            {
                "heading": "What to price instead",
                "key_point": (
                    "Cost per accepted outcome, with the acceptance rubric stated, "
                    "is defensible in a way none of the input metrics are."
                ),
            },
        ],
        "body_markdown": BODY,
        "call_to_action": "Read our weekly numbers, including the bad ones.",
        "claims": [
            {
                "statement": "No competitor publishes a cost-per-outcome figure.",
                "source_index": 0,
            }
        ],
        "sources": [sample_source(0)],
        "word_count": word_count(BODY),
    }
    payload.update(overrides)
    return payload


def sample_weekly_plan(week: dt.date = TODAY, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "week_of": week.isoformat(),
        "goal_restatement": (
            "Know who else is in the agent-infrastructure category and what they "
            "claim, publish one piece that speaks to a gap, and report on it."
        ),
        "tasks": [
            {
                "title": "Map competitor pricing disclosure",
                "assignee": "research",
                "objective": (
                    "Establish, for the four largest vendors in agent "
                    "infrastructure, whether they publish list pricing and what "
                    "unit they price in. Name the ones that do not publish, "
                    "because that absence is the story."
                ),
                "output_schema_ref": "CompetitorReport@1",
                "acceptance_criteria": [
                    "Each competitor named has a quoted source supporting the claim",
                    "Vendors that do not publish pricing are listed explicitly",
                ],
                "task_input": {"subject": "agent infrastructure pricing"},
                "due_offset_days": 2,
            },
            {
                "title": "Draft the cost-per-outcome piece",
                "assignee": "content",
                "objective": (
                    "Turn the pricing research into a blog post arguing that cost "
                    "per accepted outcome is the only defensible number, aimed at "
                    "engineering leads doing an evaluation this quarter."
                ),
                "output_schema_ref": "ContentDraft@1",
                "acceptance_criteria": [
                    "Every factual claim cites the research it came from",
                    "The audience is a named role, not a market segment",
                ],
                "task_input": {
                    "channel": "blog",
                    "depends_on_title": "Map competitor pricing disclosure",
                },
                "due_offset_days": 4,
            },
            {
                "title": "Compute the weekly numbers",
                "assignee": "analytics",
                "objective": (
                    "Produce this week's metrics report from the database so the "
                    "Friday summary has real numbers rather than impressions, "
                    "including the auto-accepted share and the coordination ratio."
                ),
                "output_schema_ref": "MetricsReport@1",
                "acceptance_criteria": [
                    "Every rate is consistent with the counts behind it",
                    "Weeks with no human sample say so in the notes",
                ],
                "task_input": {},
                "due_offset_days": 5,
            },
        ],
        "rationale": (
            "One research task feeding one content task is the smallest chain that "
            "exercises artifact flow, and the metrics task is the control."
        ),
    }
    payload.update(overrides)
    return payload


def sample_verdict(outcome: str = "ACCEPTED", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome": outcome,
        "rubric": [
            {
                "criterion": "Every competitor named is a real company",
                "met": outcome in {"ACCEPTED", "ACCEPTED_WITH_EDITS"},
                "note": "Both named vendors appear in the quoted sources.",
            }
        ],
        "reasoning": (
            "The report names two competitors, both traceable to quoted sources, "
            "and the recommendation is specific enough to start on. The pricing gap "
            "is stated honestly rather than filled in."
        ),
        "rework_instructions": (
            ["Name the two vendors that were omitted."] if outcome == "REWORK_REQUIRED" else []
        ),
        "rejection_reason": "QUALITY" if outcome == "REJECTED" else None,
        "edited_output": (sample_competitor_report() if outcome == "ACCEPTED_WITH_EDITS" else None),
    }
    payload.update(overrides)
    return payload


def sample_weekly_summary(week: dt.date = TODAY, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "week_of": week.isoformat(),
        "headline": "One piece shipped, one bounced once, metrics unaudited",
        "shipped": ["Cost-per-outcome blog post"],
        "not_shipped": ["Competitor pricing map — reworked once, still open"],
        "metrics_note": (
            "Three tasks evaluated, two accepted, one bounced. No human sample was "
            "recorded, so the acceptance numbers are unaudited this week."
        ),
        "decisions_needed": ["Approve the blog post for publication"],
        "next_week_focus": (
            "Close the pricing map and record the human sample before drawing any "
            "conclusion from the acceptance rate."
        ),
    }
    payload.update(overrides)
    return payload


def sample_metrics_report(week: dt.date = TODAY, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "period_start": week.isoformat(),
        "period_end": (week + dt.timedelta(days=6)).isoformat(),
        "submitted_tasks": 0,
        "evaluated_tasks": 0,
        "accepted_tasks": 0,
        "auto_accepted_tasks": 0,
        "rejected_tasks": 0,
        "bounced_tasks": 0,
        "total_spend_cents": 0,
        "notes": [],
    }
    payload.update(overrides)
    return payload


RESPONSES: dict[str, Any] = {
    "head.weekly_plan": sample_weekly_plan,
    "head.evaluate": lambda: sample_verdict("ACCEPTED"),
    "head.weekly_summary": sample_weekly_summary,
    "research.synthesize": sample_competitor_report,
    "content.draft": sample_content_draft,
}


# --- a provider that answers by call site and records what it was asked -------------


@dataclass
class RecordedCall:
    call_site: str
    work_class: str
    prompt: str
    system: str | None
    schema_ref: str | None


DEPARTMENT_PROVIDERS = frozenset(
    profile.provider
    for spec in DEPARTMENT_SPECS
    for profile in spec.model_profiles.profiles.values()
)
"""Which provider names the department's profiles actually resolve to.

Derived rather than written down. A harness that registered a vendor by name would
keep passing until the department moved to another one, and then fail as
`ModelCallNotAllowed: provider 'x' is not configured` raised inside a graph node —
several layers away from the one line that needed editing.
"""


@dataclass
class ScriptedProvider:
    """Answers structured calls with valid canned output; records every request.

    Not a mock of `ModelGateway`: I12 is enforced *in* the gateway, so a test that
    replaced the gateway would be asserting on its own stub. This sits one level
    lower, where a real provider would.
    """

    name: str = "scripted"
    calls: list[RecordedCall] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    fail_first: set[str] = field(default_factory=set)

    async def complete(self, profile: ModelProfile, req: ModelRequest) -> ModelResponse:
        call_site = str(req.metadata.get("call_site") or self._infer(req))
        self.calls.append(
            RecordedCall(
                call_site=call_site,
                work_class=str(req.metadata.get("work_class", "")),
                prompt=req.prompt,
                system=req.system,
                schema_ref=req.metadata.get("schema_ref"),
            )
        )
        if call_site in self.fail_first:
            self.fail_first.discard(call_site)
            text = json.dumps({"nonsense": True})
        else:
            builder = self.overrides.get(call_site) or RESPONSES.get(call_site)
            text = (
                json.dumps(builder() if callable(builder) else builder)
                if builder is not None
                else "a plain-text answer, which is what summarization returns"
            )
        return ModelResponse(
            text=text,
            provider=self.name,
            model=profile.model,
            input_tokens=max(1, len(req.prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            cost_cents=max(1, len(text) // 400),
            trust=TrustLevel.UNTRUSTED,
        )

    def _infer(self, req: ModelRequest) -> str:
        """Fall back to the schema ref when no call site was passed through.

        The gateway does not forward `call_site` in metadata — it is a gateway
        concern — so scripted answers are keyed on the schema the request asks for,
        which is unique per call site in M1.
        """
        schema = req.metadata.get("schema_ref")
        for site, builder in RESPONSES.items():
            probe = builder() if callable(builder) else builder
            if schema and isinstance(probe, dict):
                if schema.startswith("WeeklyPlan") and site == "head.weekly_plan":
                    return site
                if schema.startswith("EvaluationVerdict") and site == "head.evaluate":
                    return site
                if schema.startswith("WeeklySummary") and site == "head.weekly_summary":
                    return site
                if schema.startswith("CompetitorReport") and site == "research.synthesize":
                    return site
                if schema.startswith("ContentDraft") and site == "content.draft":
                    return site
        return "summarize.session"

    def sites(self) -> list[str]:
        return [c.call_site for c in self.calls]


# --- a whole department, wired -----------------------------------------------------


@dataclass
class M1Runtime:
    settings: Settings
    uow: UnitOfWorkFactory
    service: RunService
    worker: Worker
    relay: OutboxRelay
    streams: RedisStreams
    scheduler: Scheduler
    dispatcher: Dispatcher
    org: OrgServices
    artifacts: ArtifactStore
    provider: ScriptedProvider
    department: Department
    organization_id: OrganizationId

    @property
    def correlation(self) -> CorrelationId:
        return CorrelationId(correlation_for_week(self.organization_id, week_of()))

    async def pump(self, rounds: int = 6) -> int:
        """Dispatch → relay → execute, until nothing moves."""
        total = 0
        for _ in range(rounds):
            started = await self.dispatcher.drain()
            await self.relay.drain()
            handled = await self.worker.drain_stream(block_ms=20)
            handled += await self.worker.drain_database()
            total += handled
            if not started and not handled:
                break
        return total


async def build_m1(
    settings: Settings, organization_id: OrganizationId, *, with_triggers: bool = True
) -> AsyncIterator[M1Runtime]:
    import runtime.graphs.department
    import runtime.handlers  # noqa: F401  registers analytics@1

    uow = UnitOfWorkFactory(settings)
    department = await seed_department(uow, organization_id, with_triggers=with_triggers)

    streams = RedisStreams(settings)
    await streams.client.flushall()
    provider = ScriptedProvider()
    artifacts = ArtifactStore(uow, settings=settings)

    async with checkpointer(settings) as saver:
        worker = Worker(
            uow,
            streams,
            settings=settings,
            checkpointer=saver,
            providers=dict.fromkeys((*DEPARTMENT_PROVIDERS, "fake"), provider),
        )
        await worker.setup()
        service = RunService(uow, settings=settings)
        yield M1Runtime(
            settings=settings,
            uow=uow,
            service=service,
            worker=worker,
            relay=OutboxRelay(uow, streams, settings),
            streams=streams,
            scheduler=Scheduler(uow, service, settings=settings),
            dispatcher=Dispatcher(uow, service, settings=settings),
            org=build_org_services(uow, artifacts),
            artifacts=artifacts,
            provider=provider,
            department=department,
            organization_id=organization_id,
        )
    await streams.close()


def new_org() -> OrganizationId:
    return OrganizationId(uuid.uuid4())
