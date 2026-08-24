# M1 measurement protocol

**Frozen before the first tuning cycle, as M1 §8 requires. Changes to this document
require the same scrutiny as a schema migration, and any change resets the
clean-run clock in §4 below.**

> *"The failure mode of this milestone is not a bug. It's three weeks of prompt
> iteration ending in numbers that look fine because the definition of 'accepted'
> drifted while you were working."*

Everything here exists to stop the person who wants the answer to be yes — which is
us — from getting it by accident.

---

## Status

| | |
|---|---|
| Protocol version | 1 |
| Rubric version | 1 (`runtime/org/rubrics.py::RUBRIC_VERSION`) |
| Frozen on | 2026-08-22, before any prompt tuning |
| Tuning cycles used | 0 of 6 |
| Clean-run clock | not started |

Update the last two rows and nothing else without a corresponding version bump.

---

## 1. What "accepted" means

Not "the head agent said yes." The definition, per task type, is one paragraph each
and it lives in **`src/runtime/org/rubrics.py`** — not in this document.

That is deliberate and it is the single most load-bearing choice in the protocol.
The rubric text is used in exactly two places and it is the same text in both:

1. The manager's `EVALUATION` prompt, verbatim.
2. The human sampling harness, shown to the reviewer.

If those two ever came from different sources, a disagreement would tell you the
manager and the human disagreed about the *rubric*, not about the *work* — and the
false-accept rate, the number §8.2 says to watch, would be measuring the wrong
thing entirely. Keeping one copy in code makes divergence impossible rather than
merely discouraged.

`test_m1_schemas.py::test_every_task_output_schema_has_a_written_rubric` fails if a
task type is introduced without a written definition of acceptance, and
`rubric_for()` raises rather than falling back to a generic default.

The four rubrics, in summary — read the module for the actual text:

| task type | the thing a human must see to sign off |
|---|---|
| `CompetitorReport@1` | real, current competitors; every claim traceable to a quote that actually supports it; themes that are patterns, not restatements; one recommendation actionable this week; honest gaps rather than papered-over ones |
| `ContentDraft@1` | publishable after a copy-edit, not a rewrite; a thesis someone could disagree with; a named reader with a job; every claim cited to the research provided |
| `MetricsReport@1` | rates consistent with counts; AUTO_ACCEPTED absent from every acceptance numerator; a rate with no denominator absent rather than zero; caveats present |
| `WeeklySummary@1` | named work with named outcomes; what did not ship reported as plainly as what did; decisions stated as decisions |

---

## 2. The human sample: 20%, stratified, blind, non-negotiable

Every week, review 20% of manager-accepted tasks against the rubric, blind to the
manager's reasoning, and record the outcome as a second row in `task_evaluations`.

```bash
# draw and read this week's sample
python -m runtime.cli --organization $ORG sample

# record one review
python -m runtime.cli --organization $ORG sample \
    --record $TASK_ID --record-outcome REJECTED \
    --reason "Both 'competitors' are the same company under two brand names."

# the confusion matrix
python -m runtime.cli --organization $ORG sample --confusion
```

Four properties, each of which is a way this could be quietly useless:

**Blind.** `sample` shows the task, the artifact and the rubric. It does not show
the manager's outcome, its reasoning, its rubric scores or its edit distance, and
**there is no flag to show them.** A reviewer who has seen "accepted, all criteria
met" is not producing an independent judgement.

**Stratified by output schema.** Twenty per cent of everything would, in a week
where research produced six tasks and content produced one, review the content task
about one week in five. Each schema gets its own 20%, with a floor of one.

**Deterministic.** The selection is seeded by the week, so running the harness twice
produces the same sample. A sample that could be re-rolled until it looked better is
exactly the accidental gaming §8 exists to prevent; this makes re-rolling visibly
impossible rather than merely bad form.

**Idempotent.** `uq_evaluation_once` means a second review of the same task
conflicts rather than appending. The first review stands.

### The number to watch

**False accepts** — manager accepted, human rejected. It is on the dashboard as
`false+` and in `v_metric_dashboard.false_accept_rate`.

It matters more than agreement rate because of its direction: a false accept
*flatters every other metric while quality falls*. Acceptance goes up, rejection
goes down, rework goes down, cost per accepted outcome goes down. Every number
improves and the work gets worse. Nothing else in the system can see this happening.

### It will slip

M1 §11 risk 2 says human sampling is the first thing dropped when the week is busy
and the only thing keeping the acceptance metrics honest. So:

- Put it on the calendar as a **fixed slot**, not as a task.
- The dashboard prints a warning when the latest week has no sample, and
  `MetricsReport.notes` carries `"No human sample recorded for this week.
  Acceptance metrics are unaudited (§8.2)."` into the Friday summary a human reads.
- `WeeklyMetrics.pass_failures()` lists "no human sample was recorded" as a §9 miss,
  so a week without one cannot pass the gate however good the other numbers look.

---

## 3. Iteration budget: six cycles

A cycle is: **change prompts / schemas / decomposition → run a full week → measure.**

| # | changed | week | cost/acc | reject | coord | auto | false+ | verdict |
|---|---------|------|----------|--------|-------|------|--------|---------|
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |
| 4 | | | | | | | | |
| 5 | | | | | | | | |
| 6 | | | | | | | | |

Fill this in as you go. Count cycles publicly — an uncounted budget is not a budget.

**If the numbers are not converging by cycle six, that is the answer.** Unlimited
iteration guarantees eventual "success" and teaches you nothing: with enough cycles
you will find a prompt that happens to score well on the weeks you measured, and you
will have learned about those weeks.

Diagnose with §10 before spending a cycle. Most of it is cheap:

```bash
python -m runtime.cli --organization $ORG dashboard --json   # the four numbers
python -m runtime.cli --organization $ORG dashboard          # + the §9 gate
```

For a high coordination ratio specifically, `MetricsService.top_coordination_calls`
answers §10's "find the top three COORDINATION calls by cost" as a query rather than
a grep.

---

## 4. The clean run: two consecutive weeks, unmodified

No prompt edits. No schema changes. No hand-nudging a stuck task. No `runtime.cli
tick` — a human typing `tick` is an intervention. Run the long-lived processes and
leave them alone.

**Any intervention resets the clock.** §11 risk 1 says to budget calendar time for
at least one reset, because a task will get stuck on a Wednesday and fixing it by
hand is the obvious thing to do.

| attempt | started | reset on | reason |
|---|---|---|---|
| 1 | | | |
| 2 | | | |

What counts as an intervention, precisely, so this is not re-litigated at the time:

- editing any file under `src/` — **yes**
- editing a rubric or this document — **yes**, and it bumps the protocol version
- granting or denying an approval — **no**, that is the system working as designed
- recording a human review — **no**, that is the protocol
- restarting a crashed process without changing anything — **no**
- manually re-running a stuck task, or editing a row — **yes**
- infrastructure work that does not touch the department (a database upgrade) —
  **no**, but write it in the table above anyway

---

## 5. What gets recorded at the end

Whatever the outcome, §12 says M1 produces these. Write them down even — especially
— if the answer is no-go.

1. **Four numbers**, with the weeks they came from and the sample sizes behind them.
2. **Cost per accepted outcome**, stated as a number you would defend to someone
   paying for it. Not a range, not "roughly". The dashboard prints it in dollars.
3. **The false-accept rate**, both weeks.
4. **A go/no-go decision, recorded**, with the §10 diagnosis if it is no-go.

---

## Appendix: where each number actually comes from

Every metric is a read of a view in migration `015_metrics`. There is deliberately
no arithmetic in Python beyond assembling them — a second definition of a metric is
how a dashboard and a report come to disagree in the meeting where the decision gets
made.

| number | view | notable definition choice |
|---|---|---|
| cost per accepted outcome | `v_metric_cost_per_accepted` | numerator is *all* spend, including failed attempts and coordination; denominator excludes AUTO_ACCEPTED |
| rejection rate | `v_metric_rejection_rate` | counts tasks that were **ever bounced**, not final outcomes — a task reworked twice then accepted still cost two cycles |
| unassisted completion | `v_metric_unassisted_completion` | denominator is *evaluated* tasks; a task still in flight is not evidence either way |
| coordination ratio | `v_metric_coordination_ratio` | over **model** spend only; `overhead_ratio` is `1 - work_share`, so a mis-tagged call makes the number worse, never better |
| auto-accepted share, edit distance, agreement, false accepts | `v_metric_dashboard` | a week with no sample yields NULL, not 1.0 — "we did not look" must not render as "we agreed" |

`tests/test_m1_metrics.py` (T26) checks all of them against a fixture whose expected
values are computed by hand in the docstrings. If that ever needs changing to make a
test pass, the SQL is wrong, not the test.
