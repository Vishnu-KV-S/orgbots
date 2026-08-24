# M1 TODO — the vertical slice

Goal: prove the organization produces work a human accepts, at a defensible cost.
**This is the go/no-go gate.** Everything from M2 onward is conditional on the
numbers this milestone produces.

**Status: the machine is built and green. The measurement has not been run.**
282 tests pass (270 fast + 12 chaos/slow); ruff, ruff format, mypy strict and all
four import-linter contracts clean. M0's T0–T13 still pass unchanged, including T7
at 50 iterations × 2 durability modes.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## Before any code — fold in what M0 taught (§1)

- [x] `docs/M0_RETRO.md`, written before migration 008
- [x] **Checkpoint size** — measured (676 B avg over 4 checkpoints for a trivial
      graph). Decision: no `DeltaChannel`; state holds refs, not bodies; asserted by
      `test_m1_state_discipline.py`
- [x] **Lease timing** — 30s/10s unchanged; the independent `Heartbeater` is why.
      The real risk was `max_wall_clock_s`, now per-actor in the frozen spec
- [x] **Journal ergonomics** — `ctx.node(..., iteration=)` so a cyclic graph cannot
      collide two loop turns on one `logical_call_id`
- [x] **Leftover TODOs** — none anywhere in `src/`

## PR-14 — migrations 008–009, `SchemaRegistry`, the output schemas

- [x] `SchemaRegistry` — code registry, immutable versions, typed errors
- [x] `CompetitorReport@1`, `ContentDraft@1`, `MetricsReport@1`, `WeeklySummary@1`
- [x] `WeeklyPlan@1`, `EvaluationVerdict@1` (the head's own call outputs)
- [x] Every schema carries a **cross-field** validator — §10's "are the schemas
      actually constraining?"
- [x] Migration 008 goals/projects, 009 tasks + `runs.task_id`

## PR-15 — task repository, lifecycle, lease, assignment events

- [x] Task state machine; caps as CHECK constraints, not application logic
- [x] Task lease (`lease_worker`/`lease_until`/`version`) — distinct from the run fence
- [x] Assignment and its notification in one transaction
- [x] **Zero model calls in this PR**

## PR-16 — migration 011, inbox, dedupe, hop limit

- [x] `dedupe_key` UNIQUE; `hop_count` capped at 8 by constraint
- [x] A refused message is stored `DROPPED` with a reason, never discarded

## PR-17 — migration 013, scheduler, `trigger_key`, catch-up

- [x] Five-field cron parser (no new dependency), POSIX dom/dow rule
- [x] `trigger_fires` as dedupe table *and* catch-up ledger
- [x] `skip` / `all` policies
- [~] **Four triggers, not two.** §2's scope line names the Monday plan and the
      Friday summary; §3's loop also needs the Thursday gate and the Friday metrics.
      The machinery is identical for two or four — see `org/department.py::TRIGGERS`

## PR-18 — migration 012, sessions, summarizer

- [x] Sessions keyed `{actor}:{iso-week}`, derived so there is no allocation race
- [x] Append-only summaries with `upto_message_count`
- [~] **The summarizer is a shared graph node, not a fifth actor.** A fifth actor
      would need its own spec and runs, would put a run boundary between "the actor
      finished" and "its history was compressed", and would make the coordination
      ratio measure a department that is not the one under test

## PR-19 — `analytics`, the deterministic actor — shipped first

- [x] `analytics@1` reads the metric views → `MetricsReport@1` → task submission
- [x] Zero model calls, refused twice: `max_llm_calls = 0` **and** no profiles

## PR-20 — `research` + `web.search@1`, `web.fetch@1`

- [x] `research@1`: search → fetch → synthesize → submit → summarize
- [x] Evidence written as one artifact; state keeps an `ArtifactRefView`
- [x] Loops pass `iteration=`; a dead link is data, not an exception
- [x] `web.search@1` — the first tool with a **price**; refuses rather than
      returning an empty result set

## PR-21 — `content`

- [x] `content@1`: consumes the upstream report — artifact flow between tasks
- [x] `word_count` cross-validated against the body it ships with

## PR-22 — `marketing-head`

- [x] Four entry points, three work classes in one actor
- [x] The evaluation call is **isolated**: rubric + artifact only, no session
      summary, no recent messages (§10)
- [x] The publish gate makes no model call

## PR-23 — migrations 010 + 014, evaluation, rework cap, AUTO_ACCEPTED, approval

- [x] Manager and human verdicts in one table — the confusion matrix is a self-join
- [x] The rework cap overrides the evaluator's verdict
- [x] AUTO_ACCEPTED is a sweep, not a verdict; the schema refuses to carry it
- [x] One gate, one approver, 24h TTL, `on_expiry = deny`, no escalation chain
- [x] Enforced in `ToolGateway`, so a graph that forgot its gate node cannot publish

## PR-24 — migration 015, four metric views, dashboard

- [x] `v_task_facts`, `v_weekly_spend`, and the four metric views
- [x] `python -m runtime.cli dashboard` — exits 2 on a §9 stop threshold
- [~] **Plain views, not materialized.** §4 says materialized; a refresh job is a
      thing that can be stale on the morning somebody makes a decision

## PR-25 — human evaluation sampling harness

- [x] 20%, stratified by schema, deterministic, blind, idempotent
- [x] `sample --confusion` reads the same view the dashboard does

## PR-26 — the one real external effect, behind the gate

- [x] `publish.external@1` — IRREVERSIBLE, `retries=0`, approval-gated
- [x] Three independent switches before it publishes for real; defaults to staging
      (§11 risk 5)

---

## Tests

### Correctness — all green

| # | test | file |
|---|---|---|
| T14 | result failing schema → not SUBMITTED; typed errors | `test_m1_tasks.py` |
| T15 | 3rd schema failure → REJECTED/SCHEMA_FAILURE + escalation | `test_m1_tasks.py` |
| T16 | 3rd rework → REJECTED, not a 4th cycle | `test_m1_tasks.py` |
| T17 | deadline → AUTO_ACCEPTED, excluded from acceptance metrics | `test_m1_evaluation.py` |
| T18 | two schedulers, same minute → one run | `test_m1_scheduler.py` |
| T19 | 48h downtime + skip → one run, not 48 | `test_m1_scheduler.py` |
| T20 | A→B→A→B stops at hop 8, with a dropped row as evidence | `test_m1_inbox.py` |
| T21 | two workers claim one task → one wins | `test_m1_tasks.py` |
| T22 | approval expires → deny, terminal, counted | `test_m1_approvals.py` |
| T23 | decided twice → first wins, second recorded | `test_m1_approvals.py` |
| T24 | `analytics` attempting a model call → gateway rejection | `test_m1_actors.py` |
| T25 | every model call in all four actors carries `work_class` | `test_m1_actors.py` |
| T26 | metric views vs. hand-computed values | `test_m1_metrics.py` |

Plus: the §3 weekly loop end to end (`test_m1_weekly_loop.py`), the schema registry
and every cross-field validator (`test_m1_schemas.py`), and the M0 retro's
checkpoint budget (`test_m1_state_discipline.py`).

### Measurement — **not started**

The instrument is built and tested. The measurement is a calendar activity and it
has not been run. See `docs/MEASUREMENT_PROTOCOL.md`.

---

## Outstanding — everything left is operating, not building

- [ ] **Configure the two external tools.** `RUNTIME_SEARCH_ENDPOINT_URL` and
      `RUNTIME_PUBLISH_ENDPOINT_URL`. Both refuse rather than degrade when unset,
      which is correct but means the loop cannot do real work until they are set.
- [ ] **Provide `ANTHROPIC_API_KEY`** (or `ant auth login`). Without it the provider
      is absent and a model call fails with "provider 'anthropic' is not configured".
- [ ] **Run cycle 1** and fill in the iteration budget table.
- [ ] **Book the human-sampling slot in a calendar.** §11 risk 2.
- [ ] **The two-week clean run.** §8.4. Budget for at least one reset.
- [ ] **Record the go/no-go decision** with the four numbers behind it.

## Known limits, deliberately

- The scripted provider in `tests/conftest_m1.py` is what the end-to-end tests run
  against. They prove the loop turns and the classes land where §7 needs them; they
  prove nothing about output quality, which is what §8 is for.
- `publish.external@1` posts to a configurable endpoint. Nobody has run it against
  a real destination yet, staging included.
- `input_schema_ref` exists on `tasks` and is never set. Typed chaining between
  tasks is M2; the column is here so adding it is a backfill, not a migration on a
  hot table.
- `approvals.escalation_count` exists and is never incremented. Same reason.
- The dispatcher's `evaluate_submissions` backstop is written and not wired into a
  loop — `task.submitted` messages are the live path. It is there for the failure it
  covers (a lost message becomes an AUTO_ACCEPTED a day later) and should be
  scheduled if that is ever observed.

## Not in M1

memory · Mem0 · pgvector · the Context Engine · delegation · retrieval ·
full authority model · YAML control plane · a second department · semantic cache ·
multi-tenancy · HYBRID actors · approval escalation · model routing tiers
