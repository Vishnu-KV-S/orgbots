"""Reading the four metrics.

Everything here is a read of the migration-015 views. There is deliberately no
arithmetic in this module beyond assembling a `MetricsReport` out of numbers the
database computed — because the views *are* the definition of the four metrics, and
a second definition in Python is how a dashboard and a report come to disagree in
the meeting where the go/no-go decision gets made.

The one exception is the null handling, and it is a decision rather than a
convenience: a metric with a zero denominator comes back `None`, never `0.0`. A
week with no evaluated tasks has *no* rejection rate; rendering that as 0% would
read as a perfect week. Every rate below is `float | None` for that reason and the
dashboard prints "—" rather than a number it does not have.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from runtime.domain.ids import OrganizationId
from runtime.domain.outputs import MetricsReport
from runtime.persistence.uow import UnitOfWorkFactory


def _f(value: Any) -> float | None:
    """Postgres `numeric` → float, preserving NULL as None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _i(value: Any) -> int:
    return int(value) if value is not None else 0


@dataclass(slots=True)
class WeeklyMetrics:
    """One week, all four numbers, plus everything §7 puts on the dashboard."""

    week_start: dt.date

    total_spend_cents: int = 0
    model_cost_cents: int = 0
    tool_cost_cents: int = 0

    submitted_tasks: int = 0
    evaluated_tasks: int = 0
    accepted_tasks: int = 0
    bounced_tasks: int = 0
    rejected_tasks: int = 0
    auto_accepted_tasks: int = 0

    cost_per_accepted_cents: float | None = None
    rejection_rate: float | None = None
    unassisted_rate: float | None = None
    coordination_ratio: float | None = None
    overhead_ratio: float | None = None
    auto_accepted_share: float | None = None
    mean_edit_distance: float | None = None

    human_sampled_tasks: int = 0
    false_accepts: int = 0
    human_agreement: float | None = None
    false_accept_rate: float | None = None

    spend_by_work_class: dict[str, int] = field(default_factory=dict)

    # --- the §9 gate, as code ------------------------------------------------------

    def gate_failures(self) -> list[str]:
        """Which §9 "Stop" thresholds this week trips. Empty is good.

        Written as data rather than prose so the dashboard and the exit decision
        read the same thresholds. A number that is `None` does not trip anything —
        absence of evidence is not evidence of failure, and a week with no sample
        should say "not measured", not "failed".
        """
        stops: list[str] = []
        if self.rejection_rate is not None and self.rejection_rate > 0.60:
            stops.append(f"rejection rate {self.rejection_rate:.0%} > 60%")
        if self.auto_accepted_share is not None and self.auto_accepted_share > 0.40:
            stops.append(f"auto-accepted share {self.auto_accepted_share:.0%} > 40%")
        if self.coordination_ratio is not None and self.coordination_ratio > 0.50:
            stops.append(f"coordination ratio {self.coordination_ratio:.0%} > 50%")
        return stops

    def pass_failures(self) -> list[str]:
        """Which §9 "Pass" criteria this week does not meet."""
        misses: list[str] = []
        if self.auto_accepted_share is None or self.auto_accepted_share >= 0.20:
            misses.append("auto-accepted share is not below 20%")
        if self.rejection_rate is None or self.rejection_rate >= 0.30:
            misses.append("rejection rate is not below 30%")
        if self.coordination_ratio is None or self.coordination_ratio >= 0.25:
            misses.append(
                "coordination ratio is not below 25% "
                "(below 40% passes only with a written plan to get there)"
            )
        if self.evaluated_tasks < 30:
            misses.append(f"only {self.evaluated_tasks} evaluated tasks; §9 wants 30")
        if self.human_sampled_tasks == 0:
            misses.append("no human sample was recorded (§8.2 is non-negotiable)")
        return misses


class MetricsService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def weekly(
        self, organization_id: OrganizationId, week_start: dt.date | None = None
    ) -> list[WeeklyMetrics]:
        """Every week the data covers, or just one. Oldest first."""
        async with self._uow() as uow:
            cost = await uow.metrics.weekly(
                "v_metric_cost_per_accepted", organization_id, week_start
            )
            rejection = await uow.metrics.weekly(
                "v_metric_rejection_rate", organization_id, week_start
            )
            unassisted = await uow.metrics.weekly(
                "v_metric_unassisted_completion", organization_id, week_start
            )
            coordination = await uow.metrics.weekly(
                "v_metric_coordination_ratio", organization_id, week_start
            )
            dashboard = await uow.metrics.weekly("v_metric_dashboard", organization_id, week_start)
            spend = await uow.metrics.weekly("v_weekly_spend", organization_id, week_start)

        by_week: dict[dt.date, WeeklyMetrics] = {}

        def slot(row: dict[str, Any]) -> WeeklyMetrics | None:
            week = row.get("week_start")
            if week is None:
                # A `FULL JOIN` in v_metric_cost_per_accepted can produce a row with
                # spend but no tasks or vice versa; only a null on *both* sides
                # yields a null week, and there is nothing to attribute it to.
                return None
            return by_week.setdefault(week, WeeklyMetrics(week_start=week))

        for row in spend:
            m = slot(row)
            if m is None:
                continue
            m.total_spend_cents = _i(row["total_cost_cents"])
            m.model_cost_cents = _i(row["model_cost_cents"])
            m.tool_cost_cents = _i(row["tool_cost_cents"])
        for row in cost:
            m = slot(row)
            if m is None:
                continue
            m.accepted_tasks = _i(row["accepted_tasks"])
            m.cost_per_accepted_cents = _f(row["cost_per_accepted_cents"])
        for row in rejection:
            m = slot(row)
            if m is None:
                continue
            m.submitted_tasks = _i(row["submitted_tasks"])
            m.bounced_tasks = _i(row["bounced_tasks"])
            m.rejected_tasks = _i(row["rejected_tasks"])
            m.rejection_rate = _f(row["rejection_rate"])
        for row in unassisted:
            m = slot(row)
            if m is None:
                continue
            m.evaluated_tasks = _i(row["evaluated_tasks"])
            m.unassisted_rate = _f(row["unassisted_rate"])
        for row in coordination:
            m = slot(row)
            if m is None:
                continue
            m.coordination_ratio = _f(row["coordination_ratio"])
            m.overhead_ratio = _f(row["overhead_ratio"])
        for row in dashboard:
            m = slot(row)
            if m is None:
                continue
            m.auto_accepted_tasks = _i(row["auto_accepted_tasks"])
            m.auto_accepted_share = _f(row["auto_accepted_share"])
            m.mean_edit_distance = _f(row["mean_edit_distance"])
            m.human_sampled_tasks = _i(row["human_sampled_tasks"])
            m.false_accepts = _i(row["false_accepts"])
            m.human_agreement = _f(row["human_agreement"])
            m.false_accept_rate = _f(row["false_accept_rate"])

        for week, metrics in by_week.items():
            async with self._uow() as uow:
                metrics.spend_by_work_class = await uow.metrics.spend_by_work_class(
                    organization_id,
                    dt.datetime.combine(week, dt.time.min, tzinfo=dt.UTC),
                )
        return [by_week[w] for w in sorted(by_week)]

    async def for_week(self, organization_id: OrganizationId, week_start: dt.date) -> WeeklyMetrics:
        """One week, always a value — an empty week is zeros, not an exception."""
        weeks = await self.weekly(organization_id, week_start)
        return weeks[0] if weeks else WeeklyMetrics(week_start=week_start)

    async def total_spend_cents(self, organization_id: OrganizationId) -> int:
        async with self._uow() as uow:
            return await uow.metrics.total_spend_cents(organization_id)

    async def top_coordination_calls(
        self, organization_id: OrganizationId, since: dt.datetime, limit: int = 3
    ) -> list[dict[str, Any]]:
        """§10: "find the top three COORDINATION calls by cost"."""
        async with self._uow() as uow:
            return await uow.metrics.top_calls_by_cost(
                organization_id, "coordination", since, limit
            )

    def to_report(
        self, metrics: WeeklyMetrics, *, period_end: dt.date | None = None
    ) -> MetricsReport:
        """Shape a week into the pinned `MetricsReport@1`.

        The schema cross-validates the rates against the counts, so this method is
        also the last line of defence on the SQL: a view whose arithmetic drifted
        from its own numerator and denominator fails validation here rather than
        being rendered on a dashboard. That check is most of T26's value.
        """
        end = period_end or (metrics.week_start + dt.timedelta(days=6))
        notes: list[str] = []
        if metrics.human_sampled_tasks == 0:
            notes.append(
                "No human sample recorded for this week. Acceptance metrics are unaudited (§8.2)."
            )
        for stop in metrics.gate_failures():
            notes.append(f"STOP threshold tripped: {stop}")

        return MetricsReport(
            period_start=metrics.week_start,
            period_end=end,
            submitted_tasks=metrics.submitted_tasks,
            evaluated_tasks=metrics.evaluated_tasks,
            accepted_tasks=metrics.accepted_tasks,
            auto_accepted_tasks=metrics.auto_accepted_tasks,
            rejected_tasks=metrics.rejected_tasks,
            bounced_tasks=metrics.bounced_tasks,
            total_spend_cents=metrics.total_spend_cents,
            cost_per_accepted_cents=(
                round(metrics.cost_per_accepted_cents)
                if metrics.cost_per_accepted_cents is not None and metrics.accepted_tasks
                else None
            ),
            rejection_rate=metrics.rejection_rate,
            unassisted_completion_rate=metrics.unassisted_rate,
            coordination_ratio=metrics.coordination_ratio,
            overhead_ratio=metrics.overhead_ratio,
            auto_accepted_share=metrics.auto_accepted_share,
            mean_edit_distance=metrics.mean_edit_distance,
            human_sampled_tasks=metrics.human_sampled_tasks,
            false_accepts=metrics.false_accepts,
            human_agreement=metrics.human_agreement,
            spend_by_work_class_cents=dict(metrics.spend_by_work_class),
            notes=notes[:20],
        )
