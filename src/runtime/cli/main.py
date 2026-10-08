"""`python -m runtime.cli` — the operator's side of M1 and M2.

The M1 commands are the human interface to the weekly loop:

    seed        publish the four actors, the goal, the project and the crons
    dashboard   the four metrics, the §9 gate, and what is waiting on you
    sample      the 20% blind human review (§8.2)
    approvals   list / approve / deny the gate
    tick        drive one turn of the loop by hand

M2 adds four, and they exist because governance you cannot operate is governance you
will turn off:

    authority   what an actor may do, resolved — the answer to "why was that denied"
    denials     the §9 denial-stream review, which is an exit criterion
    killswitch  engage / disengage / list, in drain or halt
    credentials put / list, with rotation

`killswitch` is the one worth having a shell alias for. A stop that requires a
redeploy is not a stop, and the whole reason the switch moved from a dataclass to a
table in M2 is so that this command exists.

`tick` deserves a note. It exists so the loop can be exercised without running four
long-lived processes, which is what the tests do and what a developer wants. It is
**not** how the loop should run for the two-week clean period in §8.4 — a human
typing `tick` is an intervention, and §8.4 says any intervention resets the clock.
Use the long-running processes for that; use this to see whether the thing works at
all.

Exit codes are deliberate: `dashboard` exits 2 when a §9 *stop* threshold is
tripped, so a cron wrapper can page somebody rather than requiring a person to read
the number and notice.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid
from typing import Any

from runtime.cli.members import add_members_parser
from runtime.cli.memory import add_memory_parser
from runtime.cli.spec import add_spec_parser
from runtime.domain.enums import KillMode, KillScope, TaskOutcome
from runtime.domain.ids import ApprovalId, OrganizationId, TaskId
from runtime.observability.logging import configure_logging
from runtime.org.approvals import ApprovalService
from runtime.org.department import week_of
from runtime.org.metrics import MetricsService, WeeklyMetrics
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_TRIPPED = 2


def _org(value: str) -> OrganizationId:
    return OrganizationId(uuid.UUID(value))


def _cents(value: float | int | None) -> str:
    """Money, or an em dash. Never `0.00` for "we do not know".

    §7 puts "cost per accepted outcome stated as a number you'd defend" in the exit
    criteria; rendering an absent number as zero would make it indefensible in the
    most embarrassing possible way.
    """
    if value is None:
        return "—"
    return f"${value / 100:,.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


# --- seed --------------------------------------------------------------------------


async def cmd_seed(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.runtime.department_boot import seed_department

    uow = UnitOfWorkFactory(settings)
    department = await seed_department(
        uow,
        _org(args.organization),
        republish=not args.no_republish,
        with_triggers=not args.no_triggers,
    )
    print(f"organization {department.organization_id}")
    print(f"goal         {department.goal_id}")
    print(f"project      {department.project_id}")
    for name, actor_id in sorted(department.actors.items()):
        print(f"actor        {name:16} {actor_id}")
    print(f"triggers     {len(department.triggers)} installed")
    return EXIT_OK


# --- dashboard ---------------------------------------------------------------------


async def cmd_dashboard(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    metrics = MetricsService(uow)
    approvals = ApprovalService(uow)

    weeks = await metrics.weekly(org)
    if args.week:
        wanted = dt.date.fromisoformat(args.week)
        weeks = [w for w in weeks if w.week_start == wanted]
    if not weeks:
        print("no data yet")
        return EXIT_OK

    if args.json:
        print(json.dumps([_as_json(w) for w in weeks], indent=2, default=str))
    else:
        _print_table(weeks)
        latest = weeks[-1]
        print()
        _print_gate(latest)
        total = await metrics.total_spend_cents(org)
        print(f"\ntotal spend to date   {_cents(total)}")
        pending = await approvals.pending(org)
        counts = await approvals.counts(org)
        print(
            f"approvals             {len(pending)} waiting on you; "
            + ", ".join(f"{k.lower()}={v}" for k, v in sorted(counts.items()))
        )
        if latest.human_sampled_tasks == 0:
            print(
                "\n  ⚠ no human sample recorded for the latest week. §8.2 calls this "
                "non-negotiable;\n    the acceptance metrics above are unaudited "
                "until it is done."
            )

    return EXIT_GATE_TRIPPED if weeks[-1].gate_failures() else EXIT_OK


def _print_table(weeks: list[WeeklyMetrics]) -> None:
    header = (
        f"{'week':<12}{'cost/acc':>10}{'reject':>9}{'unassist':>10}"
        f"{'coord':>8}{'overhd':>8}{'auto':>8}{'eval':>6}{'sampled':>9}{'false+':>8}"
    )
    print(header)
    print("-" * len(header))
    for w in weeks:
        print(
            f"{w.week_start.isoformat():<12}"
            f"{_cents(w.cost_per_accepted_cents):>10}"
            f"{_pct(w.rejection_rate):>9}"
            f"{_pct(w.unassisted_rate):>10}"
            f"{_pct(w.coordination_ratio):>8}"
            f"{_pct(w.overhead_ratio):>8}"
            f"{_pct(w.auto_accepted_share):>8}"
            f"{w.evaluated_tasks:>6}"
            f"{w.human_sampled_tasks:>9}"
            f"{w.false_accepts:>8}"
        )


def _print_gate(week: WeeklyMetrics) -> None:
    stops = week.gate_failures()
    misses = week.pass_failures()
    if stops:
        print("§9 STOP — stop building platform and go to §10:")
        for stop in stops:
            print(f"  ✗ {stop}")
    elif not misses:
        print("§9 PASS — every criterion met for this week.")
    else:
        print("§9 — not yet passing:")
        for miss in misses:
            print(f"  · {miss}")
    if week.mean_edit_distance is not None:
        print(f"\nmean edit distance    {week.mean_edit_distance:.1f} words")


def _as_json(w: WeeklyMetrics) -> dict[str, Any]:
    return {
        "week_start": w.week_start.isoformat(),
        "cost_per_accepted_cents": w.cost_per_accepted_cents,
        "rejection_rate": w.rejection_rate,
        "unassisted_completion_rate": w.unassisted_rate,
        "coordination_ratio": w.coordination_ratio,
        "overhead_ratio": w.overhead_ratio,
        "auto_accepted_share": w.auto_accepted_share,
        "mean_edit_distance": w.mean_edit_distance,
        "evaluated_tasks": w.evaluated_tasks,
        "accepted_tasks": w.accepted_tasks,
        "submitted_tasks": w.submitted_tasks,
        "bounced_tasks": w.bounced_tasks,
        "auto_accepted_tasks": w.auto_accepted_tasks,
        "total_spend_cents": w.total_spend_cents,
        "human_sampled_tasks": w.human_sampled_tasks,
        "false_accepts": w.false_accepts,
        "human_agreement": w.human_agreement,
        "spend_by_work_class_cents": w.spend_by_work_class,
        "gate_failures": w.gate_failures(),
        "pass_failures": w.pass_failures(),
    }


# --- sample ------------------------------------------------------------------------


async def cmd_sample(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.cli.sample import SamplingHarness

    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    harness = SamplingHarness(uow)
    week = dt.date.fromisoformat(args.week) if args.week else week_of()

    if args.record:
        outcome = TaskOutcome(args.record_outcome)
        recorded = await harness.record(
            TaskId(uuid.UUID(args.record)),
            organization_id=org,
            outcome=outcome,
            reasoning=args.reason,
            reviewer=args.reviewer,
        )
        print(
            f"recorded {outcome.value} for {args.record}"
            if recorded
            else f"{args.record} was already reviewed; the first review stands"
        )
        return EXIT_OK

    if args.confusion:
        print(json.dumps(await harness.confusion(org, week), indent=2))
        return EXIT_OK

    sample = await harness.draw(org, week)
    print(
        f"week {week.isoformat()}: {len(sample.selected)} of {sample.population} "
        f"manager-accepted tasks ({sample.fraction * 100:.0f}%)"
    )
    for schema_ref, (pop, taken) in sorted(sample.by_stratum.items()):
        print(f"  {schema_ref:<24} {taken} of {pop}")
    if not sample.selected:
        print("\nnothing to review.")
        return EXIT_OK

    print()
    for item in sample.selected:
        print(await harness.render(item))
        print(
            f"\nrecord with:  python -m runtime.cli sample --organization "
            f"{args.organization} \\\n"
            f"    --record {item.task_id} --record-outcome ACCEPTED "
            f'--reason "..."\n'
        )
    return EXIT_OK


# --- approvals ---------------------------------------------------------------------


async def cmd_approvals(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    service = ApprovalService(uow)

    if args.grant or args.deny:
        approval_id = ApprovalId(uuid.UUID(args.grant or args.deny))
        won, row = await service.decide(
            approval_id,
            granted=bool(args.grant),
            decided_by=args.reviewer,
            note=args.reason,
        )
        if row is None:
            print(f"no approval {approval_id}")
            return EXIT_ERROR
        print(
            f"{'granted' if args.grant else 'denied'} {approval_id}"
            if won
            else f"already decided: {row.status.value} by {row.decided_by}; "
            "the first decision stands and yours is recorded in the audit log"
        )
        return EXIT_OK

    if args.expire:
        escalated, expired = await service.sweep()
        print(f"{len(escalated)} escalated, {len(expired)} settled by TTL")
        for row in escalated:
            print(f"  {row.id}  {row.action}  -> {row.approver}")
        for row in expired:
            print(f"  {row.id}  {row.action}  {row.status.value}")
        return EXIT_OK

    pending = await service.pending(org)
    if not pending:
        print("nothing waiting on you")
        return EXIT_OK
    now = dt.datetime.now(dt.UTC)
    for row in pending:
        left = row.expires_at - now
        hours = left.total_seconds() / 3600
        print(f"{row.id}")
        print(f"  action   {row.action}  on {row.subject_type} {row.subject_id}")
        print(f"  asked by {row.requested_by_actor}")
        print(f"  expires  in {hours:.1f}h — on expiry: {row.on_expiry}")
        print(f"  detail   {json.dumps(row.detail, default=str)[:300]}")
    return EXIT_OK


# --- M2: governance ------------------------------------------------------------------


async def cmd_authority(args: argparse.Namespace, settings: Settings) -> int:
    """What an actor may do, resolved. The answer to "why was that denied".

    Prints the resolution *source* for every action, not just the level, because
    "denied" without which policy denied it is the line that costs an hour during an
    incident. `--json` is for a script; the default rendering is for a person at 3am.
    """
    from runtime.gateway.builtin import default_action_floors
    from runtime.org.authority import AuthorityResolver

    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    resolver = AuthorityResolver(uow, action_floors=default_action_floors(settings))
    resolved = await resolver.resolve(org, args.actor)

    if args.json:
        print(json.dumps(resolved.model_dump(mode="json"), indent=2, default=str))
        return EXIT_OK

    print(
        f"{resolved.actor_name}  role={resolved.role or '—'}  "
        f"department={resolved.department or '—'}"
    )
    print(f"  default for an unlisted action: {resolved.default_level.value}")
    print("  actions:")
    for action in sorted(resolved.actions):
        entry = resolved.actions[action]
        chain = " -> ".join(entry.approver_chain) or "—"
        print(f"    {action:<24} {entry.level.value:<8} via {chain}")
        print(
            f"      {'':<24} escalations={entry.max_escalations} "
            f"ttl={entry.ttl_seconds}s on_expiry={entry.on_expiry.value}"
        )
        print(f"      {'':<24} resolved by {entry.source}")
    print("  tools:")
    for tool in sorted(resolved.tool_grants):
        connection = resolved.tool_connections.get(tool)
        credential = resolved.credential_for(tool)
        suffix = f"  via {connection} ({credential})" if connection else ""
        print(f"    {tool}{suffix}")
    if not resolved.tool_grants:
        print("    — none —")
    return EXIT_OK


async def cmd_denials(args: argparse.Namespace, settings: Settings) -> int:
    """The §9 denial-stream review, which is an exit criterion rather than a nicety.

    *"Read a week of `decision='denied'` audit rows. If an actor is repeatedly
    attempting something it lacks, either its authority is wrong or its prompt is."*
    Grouped, because a thousand identical rows answer that question worse than one row
    with a count of a thousand.
    """
    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)

    async with uow() as work:
        stream = await work.audit.denial_stream(org, since=since, limit=args.limit)
        counts = await work.audit.decision_counts(org, since=since)

    total = sum(counts.values())
    denied = counts.get("denied", 0) + counts.get("approval_required", 0)
    rate = (denied / total * 100) if total else 0.0
    print(f"last {args.days}d: {total} gateway decisions, {denied} refused ({rate:.1f}%)")
    if not stream:
        print("no denials — either everything is correctly scoped or nothing ran")
        return EXIT_OK

    print()
    print(f"{'actor':<18}{'subject':<26}{'check':<20}{'n':>5}  reason")
    for row in stream:
        print(
            f"{(row['actor'] or '—'):<18}{row['subject'][:25]:<26}"
            f"{(row['check'] or '—'):<20}{row['denials']:>5}  {(row['reason'] or '')[:60]}"
        )
    print()
    print("Each line is a finding. An actor repeatedly attempting something it lacks")
    print("means its authority is wrong or its prompt is — both worth knowing (§9).")
    return EXIT_OK


async def cmd_killswitch(args: argparse.Namespace, settings: Settings) -> int:
    """Stop things. `drain` by default, because `halt` leaves INTENT rows to reconcile."""
    from runtime.org.killswitch import KillSwitchService

    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    service = KillSwitchService(uow)

    if args.disengage:
        ok = await service.disengage(
            org,
            scope_type=KillScope(args.scope),
            scope_id=args.scope_id,
            disengaged_by=args.operator,
        )
        print("disengaged" if ok else "nothing engaged for that scope")
        return EXIT_OK if ok else EXIT_ERROR

    if args.engage:
        ok = await service.engage(
            org,
            scope_type=KillScope(args.scope),
            scope_id=args.scope_id,
            mode=KillMode(args.mode),
            reason=args.reason,
            engaged_by=args.operator,
        )
        if not ok:
            print(
                "a switch is already live for that scope. Disengage it first — two "
                "live switches in different modes is an ambiguity nobody resolves "
                "correctly under pressure."
            )
            return EXIT_ERROR
        print(f"{args.mode} engaged for {args.scope}:{args.scope_id or '*'}")
        return EXIT_OK

    live = await service.switches(org)
    if not live:
        print("nothing engaged")
        return EXIT_OK
    for switch in live:
        print(
            f"{switch.mode.value:<6} {switch.scope_type.value}:{switch.scope_id or '*'}  "
            f"by {switch.engaged_by} at {switch.engaged_at.isoformat()}  — {switch.reason}"
        )
    return EXIT_GATE_TRIPPED


async def cmd_credentials(args: argparse.Namespace, settings: Settings) -> int:
    """Store and list credentials. Never prints a secret — only its fingerprint.

    The secret is read from stdin rather than taken as an argument, because an
    argument lands in the shell history of whoever ran it and in the process table of
    everyone on the box.
    """
    from runtime.gateway.credentials import CredentialBroker, CredentialCipher

    uow = UnitOfWorkFactory(settings)
    org = _org(args.organization)
    try:
        broker = CredentialBroker(uow, CredentialCipher.from_env())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.put:
        secret = sys.stdin.read().strip()
        if not secret:
            print("read nothing from stdin; pipe the secret in", file=sys.stderr)
            return EXIT_ERROR
        version = await broker.put(
            org, name=args.put, provider=args.provider, secret=secret, rotated_by=args.operator
        )
        print(f"{args.put} is now at version {version}")
        return EXIT_OK

    async with uow() as work:
        rows = await work.credentials.list_for_org(org)
    if not rows:
        print("no credentials stored")
        return EXIT_OK
    for row in rows:
        rotated = row.rotated_at.isoformat() if row.rotated_at else "—"
        print(
            f"{row.name:<24} v{row.version:<3} {row.status.value:<8} "
            f"{row.provider:<12} key={row.key_id:<10} fp={row.fingerprint}  rotated={rotated}"
        )
    return EXIT_OK


# --- tick --------------------------------------------------------------------------


async def cmd_tick(args: argparse.Namespace, settings: Settings) -> int:
    """One turn of the crank: scheduler → dispatcher → relay → worker → sweeps."""
    import runtime.graphs.department
    import runtime.handlers  # noqa: F401  registers analytics@1
    from runtime.events.relay import OutboxRelay
    from runtime.events.stream import RedisStreams
    from runtime.graphs.checkpointer import checkpointer
    from runtime.org.department import HEAD
    from runtime.org.evaluation import EvaluationService
    from runtime.runtime.dispatcher import Dispatcher
    from runtime.runtime.run_service import RunService
    from runtime.runtime.scheduler import Scheduler
    from runtime.worker.worker import Worker

    uow = UnitOfWorkFactory(settings)
    service = RunService(uow, settings=settings)
    streams = RedisStreams(settings)
    scheduler = Scheduler(uow, service, settings=settings)
    dispatcher = Dispatcher(uow, service, settings=settings)
    approvals = ApprovalService(uow)
    evaluation = EvaluationService(uow)

    now = dt.datetime.fromisoformat(args.now) if args.now else None
    fired = await scheduler.tick(now)
    for f in fired:
        print(
            f"cron   {f.trigger:<16} {f.scheduled_for:%Y-%m-%d %H:%M} "
            f"{'skipped' if f.skipped else f.run_id}"
        )

    async with checkpointer(settings) as saver:
        worker = Worker(uow, streams, settings=settings, checkpointer=saver)
        await worker.setup()
        relay = OutboxRelay(uow, streams, settings)
        for _ in range(args.rounds):
            started = await dispatcher.drain()
            await relay.drain()
            handled = await worker.drain_stream(block_ms=50)
            handled += await worker.drain_database()
            if not started and not handled:
                break
            print(f"loop   dispatched={started} executed={handled}")

    # The full sweep, not just expiry: an approval with chain left has been ignored
    # rather than expired, and escalating it is what `max_escalations` means.
    escalated, expired = await approvals.sweep()
    auto = await evaluation.auto_accept_due(manager_name=HEAD)
    if escalated:
        print(f"sweep  {len(escalated)} approval(s) escalated to the next approver")
    if expired:
        print(f"sweep  {len(expired)} approval(s) expired")
    if auto:
        print(f"sweep  {len(auto)} task(s) AUTO_ACCEPTED — these do not count as accepted")
    await streams.close()
    return EXIT_OK


# --- wiring ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtime.cli", description=__doc__)
    parser.add_argument("--organization", required=False, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="publish actors, goal, project and crons")
    seed.add_argument("--no-republish", action="store_true")
    seed.add_argument("--no-triggers", action="store_true")
    seed.set_defaults(fn=cmd_seed)

    dash = sub.add_parser("dashboard", help="the four metrics and the §9 gate")
    dash.add_argument("--week", default=None, help="ISO date of a Monday")
    dash.add_argument("--json", action="store_true")
    dash.set_defaults(fn=cmd_dashboard)

    samp = sub.add_parser("sample", help="the 20%% blind human review (§8.2)")
    samp.add_argument("--week", default=None)
    samp.add_argument("--confusion", action="store_true")
    samp.add_argument("--record", default=None, metavar="TASK_ID")
    samp.add_argument(
        "--record-outcome",
        default=TaskOutcome.ACCEPTED.value,
        choices=[o.value for o in TaskOutcome if o is not TaskOutcome.AUTO_ACCEPTED],
    )
    samp.add_argument("--reason", default="")
    samp.add_argument("--reviewer", default="operator")
    samp.set_defaults(fn=cmd_sample)

    appr = sub.add_parser("approvals", help="the one gate")
    appr.add_argument("--grant", default=None, metavar="APPROVAL_ID")
    appr.add_argument("--deny", default=None, metavar="APPROVAL_ID")
    appr.add_argument(
        "--expire", action="store_true", help="run the sweep now: escalate, then apply TTLs"
    )
    appr.add_argument("--reason", default=None)
    appr.add_argument("--reviewer", default="operator")
    appr.set_defaults(fn=cmd_approvals)

    auth = sub.add_parser("authority", help="what an actor may do, resolved")
    auth.add_argument("actor")
    auth.add_argument("--json", action="store_true")
    auth.set_defaults(fn=cmd_authority)

    deny = sub.add_parser("denials", help="the §9 denial-stream review")
    deny.add_argument("--days", type=int, default=7)
    deny.add_argument("--limit", type=int, default=50)
    deny.set_defaults(fn=cmd_denials)

    kill = sub.add_parser("killswitch", help="engage / disengage / list")
    kill.add_argument("--engage", action="store_true")
    kill.add_argument("--disengage", action="store_true")
    kill.add_argument("--scope", default=KillScope.ORG.value, choices=[s.value for s in KillScope])
    kill.add_argument(
        "--scope-id", default=None, help="tool@version, actor name or connection name; omit for org"
    )
    kill.add_argument(
        "--mode",
        default=KillMode.DRAIN.value,
        choices=[m.value for m in KillMode],
        help="drain lets in-flight calls finish; halt does not",
    )
    kill.add_argument("--reason", default="engaged from the CLI")
    kill.add_argument("--operator", default="operator")
    kill.set_defaults(fn=cmd_killswitch)

    cred = sub.add_parser("credentials", help="store and list credentials")
    cred.add_argument(
        "--put",
        default=None,
        metavar="NAME",
        help="read the secret from stdin and store it as a new version",
    )
    cred.add_argument("--provider", default="unknown")
    cred.add_argument("--operator", default="operator")
    cred.set_defaults(fn=cmd_credentials)

    add_memory_parser(sub)
    add_members_parser(sub)
    add_spec_parser(sub)

    tick = sub.add_parser("tick", help="drive one turn of the loop by hand")
    tick.add_argument("--rounds", type=int, default=4)
    tick.add_argument("--now", default=None, help="pretend it is this ISO datetime")
    tick.set_defaults(fn=cmd_tick)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)

    needs_org = args.command in {
        "seed",
        "dashboard",
        "sample",
        "approvals",
        "authority",
        "denials",
        "killswitch",
        "credentials",
        # M3. Every memory subcommand is organization-scoped: a scope id is derived
        # from the organization, so a command without one could not build a filter and
        # would have to either fail or read across tenants.
        "memory",
    }
    # M4. `spec` is the first command whose verbs differ on this: `validate`, `show` and
    # `export` are pure functions of the files and must work in CI with no database and
    # no organization id, while `plan`, `apply` and `drift` are meaningless without one.
    # The verb's own parser says which it is, and a per-verb default beats widening the
    # set above to a command that is only sometimes in it.
    per_verb = getattr(args, "needs_org", None)
    if per_verb is not None:
        needs_org = bool(per_verb)
    if needs_org and not args.organization:
        print(
            "--organization is required for this command",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        code: int = asyncio.run(args.fn(args, settings))
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_ERROR
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
