"""`runtime.cli memory …` — the operator side of M3.

Five things a person actually has to do during this milestone, and one of them is the
milestone:

    memory status                 what is in the store, and is injection on
    memory grade                  §5's hand-grading loop, and the flip decision
    memory golden --build         §10's corpus, built from real runs
    memory evals                  the nine numbers, with their provenance
    memory promotions             the §8 queue: review, sample, decide

**`memory grade` is the one that matters and the one that will get skipped.** §13 risk
5: *"Unglamorous, no visible output, and without it every number in §11 is a guess with
a decimal point."* So it is built as an interactive loop with single-letter answers —
`h`, `n`, `x` — because a harness that requires typing "harmful" a hundred times gets a
hundred "neutral"s, neutral being the shortest word that is never wrong.

`memory flip` deliberately does **not** exist. Turning injection on is PR-35, it is an
environment variable and a worker restart, and it should not be a command that can be
run by someone who has not read the grading verdict. `memory grade --verdict` prints the
decision; a person makes it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from runtime.domain.ids import OrganizationId, PromotionId
from runtime.gateway.embeddings import EmbeddingGateway
from runtime.gateway.models import ModelGateway
from runtime.memory import MemorySubsystem
from runtime.memory.evals import render_all
from runtime.memory.golden import GOLDEN_DIR, GoldenBuilder
from runtime.memory.grading import GradingBar, grade_from_string
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_FAILED = 2
"""A distinct code from a crash, so CI can tell "the gate said no" from "the harness
broke" — the same distinction `dashboard` draws for M1's gate."""


async def _subsystem(uow: UnitOfWorkFactory, settings: Settings) -> MemorySubsystem:
    """Build the subsystem the way the worker does, minus the worker.

    `memory_enabled` is forced on for the CLI regardless of the environment: an operator
    running `memory status` to find out whether memory is on should get an answer rather
    than a subsystem that refused to construct itself. The *injection* flag is untouched
    and is what `status` reports.
    """
    forced = settings.model_copy(update={"memory_enabled": True})
    models = ModelGateway(uow, settings=forced)
    subsystem = MemorySubsystem(
        uow, models, settings=forced, embeddings=EmbeddingGateway(uow, settings=forced)
    )
    await subsystem.setup()
    return subsystem


async def cmd_memory(args: argparse.Namespace, settings: Settings) -> int:
    handlers = {
        "status": _status,
        "grade": _grade,
        "golden": _golden,
        "evals": _evals,
        "promotions": _promotions,
    }
    return await handlers[args.memory_command](args, settings)


# --- status ------------------------------------------------------------------------


async def _status(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = OrganizationId(args.organization)
    subsystem = await _subsystem(uow, settings)

    async with uow() as session:
        from sqlalchemy import text

        inventory = (
            await session.session.execute(
                text(
                    "SELECT scope, trust, status, memories, ever_read, avg_access_count "
                    "FROM v_memory_inventory WHERE organization_id = :o "
                    "ORDER BY scope, trust, status"
                ),
                {"o": org},
            )
        ).all()
        traces = (
            await session.session.execute(
                text(
                    "SELECT count(*) AS n, count(*) FILTER (WHERE grade = 'ungraded') AS ungraded, "
                    "count(*) FILTER (WHERE NOT shadow_mode) AS live "
                    "FROM context_traces WHERE organization_id = :o"
                ),
                {"o": org},
            )
        ).one()

    injecting = settings.memory_injection_enabled and settings.memory_enabled
    actors = settings.memory_injection_actors or "(all)"
    print("memory")
    print(f"  backend            {subsystem.store.backend}")
    print(f"  collection         {subsystem.store.collection}")
    print(
        f"  embedding version  {subsystem.embeddings.embedding_version} "
        f"(dim {subsystem.embeddings.dim})"
    )
    print(f"  subsystem          {'on' if settings.memory_enabled else 'OFF'}")
    print(f"  injection (PR-35)  {'ON' if injecting else 'shadow mode'}   actors: {actors}")
    print()
    if not inventory:
        print("  no memories yet")
    else:
        print(f"  {'scope':<12}{'trust':<24}{'status':<12}{'count':>7}{'ever read':>11}")
        for row in inventory:
            print(
                f"  {row.scope:<12}{row.trust:<24}{row.status:<12}"
                f"{row.memories:>7}{row.ever_read:>11}"
            )
        # §13 risk 2's early warning: a store where most memories have never been
        # retrieved is accumulating rather than remembering.
        total = sum(r.memories for r in inventory)
        read = sum(r.ever_read for r in inventory)
        if total and read / total < 0.2:
            print(
                f"\n  note: only {read}/{total} memories have ever been retrieved. "
                "A store that accumulates without being read is §13 risk 2 arriving."
            )
    print()
    print(f"  traces             {traces.n} ({traces.live} live, {traces.ungraded} ungraded)")
    if traces.ungraded:
        print("  next               runtime.cli memory grade")
    return EXIT_OK


# --- grade -------------------------------------------------------------------------


async def _grade(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = OrganizationId(args.organization)
    subsystem = await _subsystem(uow, settings)
    bar = GradingBar(
        min_helpful_or_neutral_pct=args.min_helpful,
        max_harmful_pct=args.max_harmful,
        min_sample=args.min_sample,
    )

    if args.verdict:
        passes, reasons, summary = await subsystem.grading.verdict(org, bar=bar)
        print("§5 flip decision — may injection be turned on?\n")
        for reason in reasons:
            print(f"  {reason}")
        print(f"\n  {'YES' if passes else 'NOT YET'}")
        if passes:
            print(
                "\n  To flip: set RUNTIME_MEMORY_INJECTION_ENABLED=true and restart the\n"
                "  worker. Start with one actor via RUNTIME_MEMORY_INJECTION_ACTORS.\n"
                "  This command will not do it for you — PR-35 is the only change that\n"
                "  touches a prompt, and it should be made by someone who read the above."
            )
        print(f"\n  {json.dumps(summary, default=str)}")
        return EXIT_OK if passes else EXIT_GATE_FAILED

    traces = await subsystem.grading.sample(org, size=args.count, seed=args.seed)
    if not traces:
        print("nothing to grade: no ungraded traces with retrievals")
        return EXIT_OK

    print(
        f"{len(traces)} traces to grade. h = helpful, n = neutral, x = harmful, "
        f"s = skip, q = quit.\n"
    )
    graded = 0
    for i, trace in enumerate(traces, 1):
        texts = await subsystem.store.fetch([e["memory_id"] for e in trace.retrieved])
        print("=" * 78)
        print(f"[{i}/{len(traces)}]")
        print(subsystem.grading.render(trace))
        for entry in trace.retrieved:
            body = texts.get(entry["memory_id"], "(text unavailable)")
            print(f"      {body[:160]}")
        print()
        try:
            # `to_thread`, because `input()` blocks the event loop — and this loop holds
            # a database session factory that the grading writes go through. A blocked
            # loop here is harmless today and is the kind of thing that stops being
            # harmless the moment somebody adds a heartbeat.
            answer = (await asyncio.to_thread(input, "  grade> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if answer in ("q", "quit"):
            break
        if answer in ("", "s", "skip"):
            continue
        try:
            grade = grade_from_string(answer)
        except ValueError:
            print("  not a grade; skipping")
            continue
        note = (await asyncio.to_thread(input, "  note (optional)> ")).strip() or None
        await subsystem.grading.grade(trace.id, grade=grade, graded_by=args.reviewer, note=note)
        graded += 1

    print(f"\ngraded {graded}")
    _, reasons, _ = await subsystem.grading.verdict(org, bar=bar)
    for reason in reasons:
        print(f"  {reason}")
    return EXIT_OK


# --- golden ------------------------------------------------------------------------


async def _golden(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = OrganizationId(args.organization)
    if not args.build:
        from runtime.memory.golden import load

        golden = load()
        print(f"golden set at {GOLDEN_DIR}")
        print(f"  provenance      {golden.provenance}")
        print(f"  facts           {len(golden.facts)}")
        print(
            f"  queries         {len(golden.queries)} "
            f"({sum(1 for q in golden.queries if q.get('expected_memory_ids'))} labelled)"
        )
        print(f"  contradictions  {len(golden.contradictions)}")
        print(f"  leakage probes  {len(golden.leakage)}")
        print(f"  quarantine      {len(golden.quarantine)}")
        if golden.provenance != "real":
            print(
                "\n  This is a placeholder corpus. Evals 1, 2 and 6 computed against it\n"
                "  are not evidence — see tests/fixtures/memory_golden/README.md."
            )
        return EXIT_OK

    counts = await GoldenBuilder(uow).build_from_runs(org)
    unlabelled = counts["queries"]
    print(f"built into {GOLDEN_DIR}")
    for name, count in counts.items():
        print(f"  {name:<16}{count}")
    print(
        f"\n{unlabelled} queries need hand-labelling before eval 2 will report a number.\n"
        "§10 estimates that at about a day. Fill in `expected_memory_ids` on each row,\n"
        "then set PROVENANCE to `real`."
    )
    return EXIT_OK


# --- evals -------------------------------------------------------------------------


async def _evals(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = OrganizationId(args.organization)
    subsystem = await _subsystem(uow, settings)
    results = await subsystem.evals.run_all(org)

    if args.json:
        print(json.dumps([_result_json(r) for r in results], indent=2))
    else:
        print(render_all(results))

    # The hard gates decide the exit code, and only the hard gates. Evals 1, 2, 3, 6 and
    # 7 are tracked numbers with `[CHOSEN]` bars over a corpus that may be a placeholder
    # — failing CI on one of those would be failing CI on a guess.
    gates_failed = [r for r in results if r.hard_gate and r.passes is False]
    return EXIT_GATE_FAILED if gates_failed else EXIT_OK


def _result_json(result: Any) -> dict[str, Any]:
    return {
        "eval": result.number,
        "name": result.name,
        "value": result.value,
        "bar": result.bar,
        "unit": result.unit,
        "hard_gate": result.hard_gate,
        "measured": result.measured,
        "passes": result.passes,
        "provenance": result.provenance,
        "detail": result.detail,
    }


# --- promotions --------------------------------------------------------------------


async def _promotions(args: argparse.Namespace, settings: Settings) -> int:
    uow = UnitOfWorkFactory(settings)
    org = OrganizationId(args.organization)
    subsystem = await _subsystem(uow, settings)

    if args.review:
        results = await subsystem.promotions.review_pending(org, limit=args.limit)
        approved = sum(1 for r in results if r.approved)
        print(f"reviewed {len(results)}: {approved} approved, {len(results) - approved} rejected")
        for result in results:
            mark = "+" if result.approved else "-"
            print(f"  {mark} {result.promotion_id}  {result.rationale}")
        return EXIT_OK

    if args.grant or args.deny:
        promotion_id = PromotionId(args.grant or args.deny)
        ok = await subsystem.promotions.decide(
            org,
            promotion_id,
            approve=bool(args.grant),
            reviewer=args.reviewer,
            reviewer_kind="human",
            rationale=args.reason or "decided from the CLI",
        )
        print("decided" if ok else "not found, or already decided")
        return EXIT_OK if ok else EXIT_ERROR

    if args.sample:
        rows = await subsystem.promotions.sample_for_human(org, limit=args.limit)
        print(f"{len(rows)} model-approved promotions to check by hand (§8)\n")
        for row in rows:
            print(f"  {row['promotion_id']}  {row['from_scope']} -> {row['to_scope']}")
            print(f"    {row['text'][:150]}")
            print(f"    why: {row['rationale']}")
            print(f"    criteria: {row['criteria']}\n")
        return EXIT_OK

    async with uow() as session:
        pending = await session.promotions.pending(org, limit=args.limit)
    texts = await subsystem.store.fetch([p.memory_id for p in pending])
    print(f"{len(pending)} proposals awaiting review\n")
    for proposal in pending:
        print(
            f"  {proposal.id}  {proposal.from_scope.value} -> "
            f"{proposal.to_scope.value}  by {proposal.proposed_by}"
        )
        print(f"    {texts.get(proposal.memory_id, '(text unavailable)')[:150]}\n")
    if pending:
        print("  --review runs the classifier; --grant/--deny decide one by hand")
    return EXIT_OK


# --- parser ------------------------------------------------------------------------


def add_memory_parser(sub: Any) -> None:
    memory = sub.add_parser("memory", help="M3: the store, grading, evals and promotions")
    inner = memory.add_subparsers(dest="memory_command", required=True)

    status = inner.add_parser("status", help="what is in the store, and is injection on")
    status.set_defaults(fn=cmd_memory)

    grade = inner.add_parser("grade", help="§5's hand-grading loop and the flip decision")
    grade.add_argument("--count", type=int, default=25)
    grade.add_argument("--seed", default="m3", help="the sample is deterministic in this")
    grade.add_argument("--reviewer", default="operator")
    grade.add_argument(
        "--verdict",
        action="store_true",
        help="print the §5 bar check instead of grading. Exit 2 if the bar is not met.",
    )
    # The bars are arguments so that a deployment with its own §5 position can pass it,
    # and defaults so that the shipped one is the documented one. They are `[CHOSEN]`
    # either way; what matters is that the number is set before the data is seen.
    grade.add_argument("--min-helpful", type=float, default=GradingBar().min_helpful_or_neutral_pct)
    grade.add_argument("--max-harmful", type=float, default=GradingBar().max_harmful_pct)
    grade.add_argument("--min-sample", type=int, default=GradingBar().min_sample)
    grade.set_defaults(fn=cmd_memory)

    golden = inner.add_parser("golden", help="§10's corpus: inspect, or build from real runs")
    golden.add_argument("--build", action="store_true")
    golden.set_defaults(fn=cmd_memory)

    evals = inner.add_parser("evals", help="the nine evals (§11). Exit 2 if a hard gate fails.")
    evals.add_argument("--json", action="store_true")
    evals.set_defaults(fn=cmd_memory)

    promotions = inner.add_parser("promotions", help="the §8 review queue")
    promotions.add_argument("--review", action="store_true", help="run the cheap classifier")
    promotions.add_argument("--sample", action="store_true", help="what the model approved")
    promotions.add_argument("--grant", default=None, metavar="PROMOTION_ID")
    promotions.add_argument("--deny", default=None, metavar="PROMOTION_ID")
    promotions.add_argument("--reason", default=None)
    promotions.add_argument("--reviewer", default="operator")
    promotions.add_argument("--limit", type=int, default=20)
    promotions.set_defaults(fn=cmd_memory)


__all__ = ["add_memory_parser", "cmd_memory"]
