# M1 Implementation Plan — The Vertical Slice

**Prerequisite:** M0 exit tests green (T7 ×50 on both runners, R1 green).
**Goal:** prove the organization produces work a human accepts, at a defensible cost.
**Duration:** 5–7 weeks.
**This is the go/no-go gate.** Everything from M2 onward is conditional on the numbers this milestone produces.

M0 asked *does the runtime execute correctly?* M1 asks *does any of this deserve to exist?* Those are different kinds of question and they need different kinds of discipline. M0 was pass/fail on tests. M1 is a measurement, and measurements can be gamed — mostly by accident, by the person who wants the answer to be yes. §8 exists to stop that.

---

## 1. Before writing any code: fold in what M0 taught you

Spend the first half-day on this, not on migration 008.

- **Checkpoint size.** If `echo_agent` checkpoints were already larger than expected, M1's graphs will be worse. Decide now whether `DeltaChannel` goes in, or whether state fields get stricter discipline.
- **Lease timing.** If 90s leases needed adjusting under real tool latency, fix the default before four actors are running concurrently.
- **Journal ergonomics.** If `logical_call_id` needed a wrapper or the `ordinal` was awkward to thread through nodes, fix the ergonomics now. Four graphs will encode whatever pattern you settle on.
- **Anything you wrote a `TODO` for.** M0's definition of done said zero TODOs in `effects/` and `run_service.py`. If any leaked in, they get paid down here, not in M6.

Write these findings down as an M0 retro in the repo. They're the only thing standing between "the plan was correct" and "the plan is still correct."

---

## 2. Scope

**In:**

- Four actors: `marketing-head` (LLM), `research` (LLM), `content` (LLM), `analytics` (DETERMINISTIC)
- One company goal, one project, real tasks
- Tasks with pinned input/output schemas and validation gating `SUBMITTED`
- Evaluation: `task_evaluations`, the outcome state machine, `AUTO_ACCEPTED` handling, rework cap
- Inbox with `correlation_id`, `hop_count`, `dedupe_key`, `artifact_id` payloads
- Sessions + session summaries
- Two cron triggers: Monday plan, Friday summary
- Minimal approval: one gate, one human approver, TTL, no escalation chain
- One real external effect behind that gate
- Artifacts flowing through tasks and messages
- **All four metrics as SQL views, plus `work_class` on every model call from the first one**
- Human evaluation sampling harness

**Out — and these are the ones that will be tempting:**

```
memory / Mem0 / pgvector      the Context Engine       delegation
retrieval of any kind          full authority model     YAML control plane
a second department            semantic cache           multi-tenancy
HYBRID actors                  approval escalation      model routing tiers
```

If M1 needs long-term memory to hit its numbers, that is a finding, not a scope change. Write it down and keep going.

### Deliberate simplifications, stated so nobody "fixes" them

**Context assembly is a fixed template, not an engine.** System prompt + actor spec block + task input (validated object) + session summary + last 6 messages + referenced artifact summaries. No retrieval, no reranking, no adaptive budget. If token counts get uncomfortable, tighten the template — don't build M3 early.

**Authority is a hardcoded dict, not a resolver.** Three entries: `publish_external` → human, everything else → auto. The full model is M2.

**Approvals have no escalation chain.** One approver (you), a 24h TTL, `on_expiry = deny`. Denials on expiry are counted and reported — a run blocked by an unanswered approval is a real signal about operating cost.

---

## 3. The weekly loop

This is the artifact M1 must produce. Everything else is scaffolding.

```
MON 08:00  cron → marketing-head.weekly_plan
             loads: goal, last week's metrics artifact, open tasks, inbox
             WORK:         none
             COORDINATION: plan + task decomposition (1–2 model calls)
             writes 3–6 tasks with pinned output schemas, assigns them
             → task.assigned events (deterministic, zero model calls)

MON–THU    each assignee wakes on task.assigned
             research:  WORK — search, fetch, synthesize → CompetitorReport
             content:   WORK — draft → ContentDraft
             analytics: DETERMINISTIC — query, compute, render → MetricsReport
             each: validate against schema → artifact → SUBMITTED
             → task.submitted event

             marketing-head wakes on task.submitted
             EVALUATION: accept / accept-with-edits / rework / reject
             rework → back to ASSIGNED (max 2)

THU 16:00  publish gate (if any content reached ACCEPTED)
             authority: publish_external → human
             approval created, 24h TTL
             you approve or it expires

FRI 16:00  cron → analytics.weekly_metrics    DETERMINISTIC, zero model calls
FRI 17:00  cron → marketing-head.weekly_summary
             COORDINATION/SUMMARIZATION: one artifact for the human
```

Note what has **no** model calls: task assignment, submission notification, dependency completion, metric computation, the whole event fabric. That's not an optimization, it's the thesis from v3 §17 — if the coordination ratio comes out high anyway, the hierarchy is the problem, not the plumbing.

---

## 4. Migrations

```
008_goals          goals, projects
009_tasks          tasks (input_schema_ref, output_schema_ref, outcome,
                          rework_count, lease_worker, lease_until, version)
010_evaluations    task_evaluations
011_inbox          inbox_messages
012_sessions       sessions, session_summaries
013_triggers       triggers, trigger_fires
014_approvals      approvals (minimal: no escalation_count usage yet)
015_metrics        materialized views for the four metrics
```

Schemas live in a **code registry**, like tools — `SchemaRegistry.register(CompetitorReport, version=1)`. Tasks store the ref string (`"CompetitorReport@1"`). No schema table. A registered schema version is immutable; changing fields means `@2`.

Key columns beyond v3 §11:

```sql
-- 009
input_schema_ref   text,          -- nullable now, typed chaining later
output_schema_ref  text NOT NULL,
outcome            text,          -- NULL until evaluated
schema_failures    int NOT NULL DEFAULT 0,
submitted_at       timestamptz,
evaluated_at       timestamptz,
eval_deadline      timestamptz    -- drives AUTO_ACCEPTED
```

```sql
-- 013
CREATE TABLE trigger_fires (
    trigger_key   text PRIMARY KEY,   -- H(trigger_id, scheduled_for_utc)
    trigger_id    uuid NOT NULL,
    scheduled_for timestamptz NOT NULL,
    fired_at      timestamptz NOT NULL DEFAULT now(),
    run_id        uuid
);
```

`trigger_fires` doubles as the dedupe table and the catch-up ledger. Racing schedulers, redelivery, and merged wakes all collapse into one primary-key conflict.

---

## 5. Build order

| PR | Content | Notes |
|---|---|---|
| 14 | Migrations 008–009; `SchemaRegistry`; the four output schemas | Write the schemas **first** — they define what the agents must produce |
| 15 | Task repository, lifecycle, lease, assignment events | Zero model calls in this PR |
| 16 | Migration 011, inbox, dedupe, hop limit | |
| 17 | Migration 013, scheduler, `trigger_key`, catch-up policy | Two crons |
| 18 | Migration 012, sessions, summarizer worker (`work_class=SUMMARIZATION`) | |
| 19 | `analytics` deterministic worker → `MetricsReport` artifact | **Ship the non-LLM actor first** — it's the one that must work |
| 20 | `research` graph + `web.search@1`, `web.fetch@1` → `CompetitorReport` | First real WORK-class calls |
| 21 | `content` graph → `ContentDraft` | |
| 22 | `marketing-head` graph: plan, evaluate, summarize | Three distinct `work_class` values in one actor |
| 23 | Migration 010 + 014; evaluation states; rework cap; `AUTO_ACCEPTED`; minimal approval gate | |
| 24 | Migration 015; four metric views; dashboard | Must land before tuning starts (§8) |
| 25 | Human evaluation sampling harness (CLI is fine) | |
| 26 | The one real external effect, behind the gate | Last, deliberately |

PR-19 before the LLM actors is intentional. If the deterministic path can't produce a validated artifact and get it evaluated, the LLM path won't either, and you'll have spent two weeks debugging prompts to discover a task-lifecycle bug.

---

## 6. Tests

Two categories, and conflating them is a mistake.

### Correctness (binary, CI, red-first)

| # | Test | v3 |
|---|---|---|
| T14 | Task result failing schema → not `SUBMITTED`; typed errors returned | 36 |
| T15 | 3rd schema failure → `REJECTED / SCHEMA_FAILURE` + escalation | 36 |
| T16 | 3rd rework → `REJECTED`, not a 4th cycle | 35 |
| T17 | Eval deadline passes → `AUTO_ACCEPTED`, **excluded from acceptance metrics** | 32, 33 |
| T18 | Two schedulers, same cron minute → one run (`trigger_key` conflict) | 52 |
| T19 | 48h downtime + `skip` policy → one run, not 48 | 53 |
| T20 | Message loop A→B→A → stops at `hop_count` 8 | 55 |
| T21 | Two workers claim one task → one wins (lease + `version`) | 58 |
| T22 | Approval expires → `deny`, run terminal, counted | 25 |
| T23 | Approval decided twice → first wins, second recorded | 26 |
| T24 | `analytics` attempting a model call → gateway rejection | 63 |
| T25 | Every model call in all four actors carries `work_class` | I12 |
| T26 | Metric views: seeded fixture → hand-computed expected values | — |

T26 matters more than it looks. If the metric SQL is wrong, every decision downstream is wrong, and nothing else will catch it.

### Measurement (not pass/fail — these produce the gate numbers)

Run continuously from the first working loop. They aren't tests; they're the instrument.

---

## 7. The four metrics, as SQL

Define these in PR-24, before any prompt tuning. Sketches, not final:

```sql
-- Cost per accepted outcome
SELECT sum(u.actual_cost_cents)::float
       / nullif(count(DISTINCT t.id) FILTER (
             WHERE t.outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS')), 0)
FROM usage_ledger u FULL JOIN tasks t ON t.id = u.task_id
WHERE u.created_at >= :from;
-- AUTO_ACCEPTED deliberately absent from the denominator

-- Rejection rate
count(*) FILTER (WHERE outcome IN ('REJECTED','REWORK_REQUIRED'))::float
  / nullif(count(*) FILTER (WHERE submitted_at IS NOT NULL), 0)

-- Unassisted completion rate
count(*) FILTER (WHERE outcome = 'ACCEPTED' AND human_touched = false)::float
  / nullif(count(*), 0)

-- Coordination ratio
sum(cost) FILTER (WHERE work_class = 'COORDINATION')::float / sum(cost)
-- report overhead_ratio = all non-WORK / total alongside it
```

Also on the dashboard, non-negotiably: `AUTO_ACCEPTED` share, mean `edit_distance` trend, human-vs-manager agreement, and total spend to date.

---

## 8. Freeze the measurement protocol before you tune

The failure mode of this milestone is not a bug. It's three weeks of prompt iteration ending in numbers that look fine because the definition of "accepted" drifted while you were working.

Write these down in the repo **before the first tuning cycle** and treat changes to them as requiring the same scrutiny as a schema migration:

1. **What "accepted" means.** Not "the head agent said yes." Write, per task type, what a human would have to see to sign off. One paragraph each. This is the rubric your sampling uses.
2. **The human sample is 20%, stratified, and non-negotiable.** Every week, review 20% of manager-accepted tasks against the rubric, blind to the manager's reasoning. Record your outcome as a second row in `task_evaluations`. This is what makes the confusion matrix real — and **false accepts** (manager accepted, you reject) are the number to watch, because they flatter every other metric while quality falls.
3. **An iteration budget.** Give yourself a fixed number of tuning cycles — six is reasonable — where a cycle is: change prompts/schemas/decomposition, run a full week, measure. If the numbers aren't converging by cycle six, that's the answer. Unlimited iteration guarantees eventual "success" and teaches you nothing.
4. **The clean run is two consecutive weeks, unmodified.** No prompt edits, no schema changes, no hand-nudging a stuck task during those two weeks. Any intervention resets the clock. This is the hardest rule to keep and the only one that makes the result mean anything.

---

## 9. Exit criteria

**Pass, all of:**

- The weekly loop ran unattended for two consecutive weeks with no code or prompt changes
- Four metrics computable, with at least 30 evaluated tasks behind them
- `AUTO_ACCEPTED` share below 20%
- Rejection rate below 30%
- Coordination ratio below 25% (or below 40% with a written plan to get there)
- Human sample completed both weeks; false-accept rate recorded
- Cost per accepted outcome stated as a number you'd defend to someone paying for it

**Stop, any of:**

- Rejection rate above 60%
- `AUTO_ACCEPTED` above 40%
- Coordination ratio above 50%
- Iteration budget exhausted without convergence

Stop means *stop building platform*, not stop the project. §10 is where you go.

---

## 10. If the numbers are bad — diagnose before you rebuild

Resist the reflex to add memory or a better model. Work through this in order; each step is cheap and rules out a cause.

**High rejection rate →**
- Read 10 rejected outputs. Are they wrong, or right-but-not-what-was-asked? Wrong is a capability problem. Not-what-was-asked is a **task decomposition** problem, and it's the common one.
- Are the output schemas actually constraining? A `recommendation: str` with `min_length=50` permits garbage. Tighten schemas before touching prompts — it's free and it's measurable.
- Is the head agent's task description carrying enough context, or is the assignee guessing? Look at the actual `input` JSON on rejected tasks.

**High auto-acceptance →** you aren't evaluating. Either the head agent's evaluation node is failing silently, or the eval deadline is too short. Check `task_evaluations` row counts against `submitted_at` counts.

**High false-accept rate →** the head agent is a rubber stamp. Its evaluation prompt needs the rubric from §8.1 verbatim, and the evaluation call needs to be a separate model call with only the artifact and the rubric in context — not a continuation of the conversation where it assigned the work.

**High coordination ratio →** find the top three `COORDINATION` calls by cost. Ask of each: could a rule do this? Task assignment, notification, and status rollup should already be rule-based. If planning is the cost, decompose weekly instead of daily. If evaluation is the cost, that's `EVALUATION` class and it's often worth the money — check the overhead ratio separately before cutting it.

**High cost per accepted outcome, everything else fine →** this is the good failure. Route summarization and evaluation to a cheaper model, compress artifact summaries, cut the last-6-messages window. That's M2/M3 work with a clear target.

Only after all of the above: consider whether the work genuinely needs memory or retrieval. If it does, that's a real finding — M3 gets pulled forward and M1's gate is re-run after it.

---

## 11. Risks specific to M1

1. **The two-week clean run will be tempting to interrupt.** A task gets stuck on a Wednesday and you fix it by hand. The clock resets. Budget calendar time for at least one reset.
2. **Human sampling will slip.** It's the first thing dropped when the week is busy, and it's the only thing keeping the acceptance metrics honest. Put it on the calendar as a fixed slot, not as a task.
3. **Prompt tuning expands to fill available time.** The iteration budget is the guard. Count cycles publicly.
4. **The head agent evaluating its own department is a closed loop.** The 20% sample is the only opening in it.
5. **Real external effects create real consequences.** PR-26 is last for a reason. Point it at a staging destination for the first week regardless of what the approval gate says.

---

## 12. What M1 produces

- A running four-actor department
- Four numbers, defensible, with the evaluation protocol that produced them
- A written answer to: *what does one accepted outcome cost, and what share of that is agents talking to each other?*
- A go/no-go decision, recorded, with the diagnosis if it's no-go

Not: a platform. Not: reusable abstractions. Not: a second department. The hardcoding is the point — it's what makes the measurement fast enough to be honest.

---

*Next document is the M2 plan, written only if the gate passes. If it doesn't, the next document is a findings memo and a revised M1.*
