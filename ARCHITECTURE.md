# Architecture

> **Status of this file.** The M0 plan requires that `ARCHITECTURE.md` contain the
> fourteen invariants **verbatim** from the frozen v3 architecture document. That
> document was not part of the M0 implementation brief, so the statements below are
> **reconstructed** from how the invariants are cited in the plan (I3/I4 gateways,
> I8 budget, I12 work class) and from what the code actually enforces.
>
> **Before M0 is signed off, replace §2 with the verbatim v3 text.** Anywhere the
> reconstruction and v3 disagree, v3 wins and the code is wrong until it matches.
> Each invariant below names the test that holds it, so a wording correction that
> changes the meaning will show up as a test that no longer fits.

---

## 1. What M0, M1, M2, M3, M4 and M5a are

M0 proves runtime **correctness**. M1 asks whether any of it **deserves to exist**.
M2 makes it **safe to point at things that matter** — and is the first milestone that
can fail by being too expensive rather than by being wrong. M3 gives actors
**continuity**, and is the first milestone that can fail by making the output *worse*.
M4 moves the organization into YAML and is the only milestone whose exit criterion is
that **nothing changed** — provable, rather than argued, because the compiled
`spec_hash` values are byte-identical to the Python ones. M5a lets an actor **spawn
bounded child work**, and is the first milestone that *multiplies* whatever the
organization already gets wrong: a parent that decomposes badly now produces three
children that decompose badly. So it ships dark, like M3, and its second half — a
second department — is gated on M1's numbers rather than on its own code being ready.

Those are different kinds of question and they need different kinds of discipline:
M0 was pass/fail on tests, M1 is a measurement, and a measurement can be gamed by
the person who wants the answer to be yes. `docs/MEASUREMENT_PROTOCOL.md` is what
stops that, and it is frozen.

**M0's exit criterion** is one sentence: *kill a worker mid-tool-call; the run
resumes and the effect journal proves the tool executed exactly once.* Three
mechanisms carry almost all of the weight:

| Mechanism | Lives in | Without it |
|---|---|---|
| Effect journal | `effects/journal.py` | A crash mid-tool-call re-fires the effect on replay |
| Lease + fence | `worker/lease.py` | A frozen worker thaws and commits over its replacement |
| Transactional outbox | `events/relay.py` | "The run exists" and "the run was announced" disagree |

If M0 ran out of time, the M0 plan says to cut `hasher` and the artifact store
before cutting either of the first two. That ordering is the architecture in one
line.

**M1's exit criterion** is a set of numbers rather than a sentence: the weekly loop
running unattended for two consecutive weeks, four metrics computable over at least
thirty evaluated tasks, `AUTO_ACCEPTED` under 20%, rejection under 30%, coordination
ratio under 25%, the human sample completed, and a cost per accepted outcome you
would defend to someone paying for it. §5 below is what M1 adds to make that
measurable; `TODO_M1.md` tracks what is built against what is still to be run.

**M2's exit criterion has two halves and the second is the unusual one.** The first is
ordinary: T27–T43 green, and the injection corpus results documented including the
payloads that get through. The second is **non-regression** — one full week with every
governance mechanism active, and cost per accepted outcome within +10% of M1's,
latency within +25%, and no other metric worse. Everything M2 adds costs something,
and governance that makes the organization 40% more expensive has not made it safer;
it has made it unaffordable, and that argument wins eventually. §8 below is what M2
adds; `TODO_M2.md` tracks it and `docs/M2_GATE.md` is the worksheet for the week.

---

## 2. The fourteen invariants

*Reconstructed — see the status note above.*

**I1 — One door.** A run is created only by `RunService.start_run()`. Nothing else
inserts into `runs`.
→ `runtime/run_service.py`; T2.

**I2 — A logical call has a stable identity.** `logical_call_id` is a pure function
of `(run_id, checkpoint_ns, node, ordinal, args_hash)`. It contains no clock, no
randomness, and no fence, so a replayed node reproduces it exactly.
→ `effects/journal.py::logical_call_id`; T6.

**I3 — Every external call goes through a gateway.** Graphs and handlers reach the
outside world through `ToolGateway` and `ModelGateway` or not at all.
→ `.importlinter` contract `gateway-only`; enforced at CI, not by review.

**I4 — Gateways are the only place credentials, retries and audit live.** A caller
cannot opt out of the pipeline by constructing its own client.
→ same contract; `gateway/tools.py`.

*M3 is where this was nearly lost.* Mem0 constructs its own LLM, embedder and connection
pool, so a memory subsystem built straight on it would have made extraction, fusion and
embedding — the highest-volume model calls in M3 — the only ones with no credential
broker and no audit row. `memory/mem0_store.py` re-registers both of Mem0's factories to
route back through the gateways instead. → `docs/M3_LIBRARY_FACTS.md` fact 7.

**I5 — One run, one owner.** A run is executed by exactly one worker at a time,
established by a conditional claim rather than a read-then-write.
→ `persistence/repositories/runs.py::claim`; T4.

**I6 — A fence is checkable after the fact.** Every gateway entry re-reads the
authoritative fence and refuses if the run has moved on.
→ `worker/lease.py::check`; T5.

**I7 — Intent is durable before the effect.** The INTENT row commits on its own
connection before any external mutation, so a crash always leaves an interpretable
state.
→ `effects/journal.py::begin`; T7.

**I8 — Budget cannot be exceeded.** `committed_cents + reserved_cents <=
limit_cents`, enforced by a database CHECK constraint, not by application logic.
→ migration `006_budget`; T11.

*M2 makes this per level.* The constraint is unchanged and still on every row; what
changed is that a reservation now holds against every pool from the run's leaf to the
org root simultaneously, so I8 holds at each. The single path is
`BudgetRepository.reserve_chain`, locking `ORDER BY depth ASC, id ASC` with
`FOR NO KEY UPDATE`. → T27, and `.importlinter`'s `budget-tables-are-private`.

**I9 — Announcements are transactional.** An event is written in the same
transaction as the state change it describes, and published at-least-once with
idempotent delivery.
→ `events/relay.py`; T3.

**I10 — Redis is reconstructible.** Redis holds no state that cannot be rebuilt
from Postgres. A `FLUSHALL` loses nothing.
→ `worker/worker.py::drain_database`; T8/R1.

**I11 — A run executes under a frozen spec.** The worker loads `RunSpec` from
`run_specs`, never from live configuration.
→ `runtime/run_service.py`, `worker/executor.py::_load_spec`; T2.

*M2 freezes authority into it too*, so `spec_hash` covers what the run was allowed to
do and not merely what it was allowed to call. The one thing deliberately outside the
freeze is **revocation**, which must take effect inside a running run and does so
through a ≤30s permission re-check rather than by mutating a spec. → T36.

**I12 — Every model call declares its work class.** `work_class` selects the model
profile, is the unit the usage ledger aggregates on, and is required.
→ `gateway/models.py::complete`; T10.

*M3 adds `MEMORY` and an entry point without a run.* `complete_detached` exists because
the memory worker runs after a run has finished and has no `RunContext` to carry — but
it still requires a work class, still reserves budget, still checks the kill switch and
still writes a decision row. The one thing it cannot do is enforce per-run ceilings,
because there is no run; the budget pool is the bound and the batch size is the
backstop. `EmbeddingGateway` is the same shape for embeddings.

**I13 — A recovery policy must be entailed by capabilities.** A tool cannot declare
a recovery it has no means to perform. Checked at registration; failure is a
refusal to start.
→ `effects/policies.py::check_entailment`; T9.

**I14 — Blast radius sets policy, and a tool may only tighten it.** `read` → auto;
`reversible` → auto under a threshold; `irreversible` → approval, `retries=0`,
elevated audit, allow-list only.
→ `effects/policies.py::resolve_policy`; T13, T39.

*M2 extends this to authority.* The blast-radius floor is the last step of the
resolution chain, so a policy row claiming an irreversible action is `auto` loses to
the tool registry — which is the authority on what a tool does to the world. Waiving
it is a code change to the allow-list, not a config row.
→ `domain/authority.py::apply_floor`.

---

## 3. Layers

Enforced by `import-linter`, top to bottom. An arrow may only point downwards.

```
cli                (operator commands; nothing imports it)
api
worker
spec               (M4: the config plane — loader, compiler, differ, applier)
runtime            (RunService, lifecycle, scheduler, dispatcher, sweeper)
graphs | handlers
memory             (M3: store, planner, extraction, promotion, evals)
gateway            (tools, models, embeddings, credentials, ratelimit, governance)
org                (M1: tasks, inbox, sessions, evaluation, approvals, metrics)
                   (M2: authority resolver, kill switch)
effects | budget | artifacts | events
persistence
domain             (pure: Pydantic, enums, errors, hashing, schemas, authority,
                    trust, memory, scrub — no I/O)
```

`runtime.memory` is M3's addition and both of its edges are load-bearing. It sits
**below the graphs** because a graph node is what asks for context — `ContextPlanner`
hands back a rendered block and `graphs.common.context` puts it in the template. It sits
**above the gateway** because every model call and every embedding it makes has to go
through one: extraction, fusion and promotion review are all `work_class=MEMORY` and
carry a budget reservation, a kill-switch check and an audit row like everything else.
Left to itself, Mem0 constructs its own LLM and embedder clients, which would make M3's
highest-volume model calls the only ungoverned ones in the system.

`runtime.spec` is M4's, and both of its edges matter too. It sits **above the
runtime** because compiling a document set needs the cron parser and the graph and
handler registries — the same imports `department_boot` makes, for the same reason: a
reference that does not resolve should fail an apply rather than a cron three days
later. It sits **below the worker and the API** because nothing that executes a run may
reach the config plane. A run executes under its frozen `RunSpec`; a worker that could
import the compiler could be made to consult live configuration, which is the one
property M0 built the RunSpec to prevent. The CLI is the only caller.

`runtime.org` sits **below** the gateway, which is the one placement worth
explaining. The tool gateway consults authority and approvals before an irreversible
call, so it must reach them downwards; a gateway importing policy from above would
invert the stack. The graphs above get the same services without a second copy.

`runtime.domain` gaining `authority` and `trust` is worth a note, because both sound
like policy and policy usually lives higher. They are here because both are *values*:
a `ResolvedAuthority` is the frozen answer that gets hashed into `spec_hash`, and a
fence is a pure function of its content. The things that *decide* — the resolver, the
gateway — sit where they always did. What moved down is only what has to be
immutable and I/O-free.

Four additional contracts:

- **gateway-only** — `runtime.graphs`, `runtime.handlers`, `runtime.runtime` and
  `runtime.org` may not *directly* import `httpx`, `openai`, `anthropic`, `boto3`
  or `redis`. They
  reach those libraries through the gateway, which is the design; holding a client
  of their own is the violation.
- **domain-purity** — `runtime.domain` imports no I/O library and no other runtime
  package.
- **budget-tables-are-private** (M2) — nothing above `runtime.budget` may import the
  budget repository. `reserve_chain` is the only path that locks the pool chain in
  `depth ASC, id ASC` order, and a second path locking in any other order deadlocks
  against it under exactly the concurrency T27 exercises. The import graph is half the
  guard; `test_m2_governance.py` scans the source for raw SQL against `budget_pools`,
  because a direct `session.execute(text(...))` needs no import at all and is precisely
  the "quick UPDATE for a special case" that M2 §10 warns about.

Run them with `lint-imports`. They fail CI, which is the point: I3 and I4 are the
kind of rule that decays under deadline pressure unless a machine holds it.

---

## 4. The shape of one run

```
POST /v1/runs {actor, input, idempotency_key}
  │
  ├─ resolve authority (cached ≤30s), check the kill switch
  ├─ TX BEGIN
  │    resolve actor → active version → compile spec (authority in the hash)
  │    INSERT runs (QUEUED, fence=0)
  │    ensure the pool chain: org → department → actor
  │    admit: hard + soft + priority, at every level
  │      └─ refused? UPDATE runs → LIMIT_REACHED, outbox 'run.refused', COMMIT, done
  │    INSERT budget_allocations (advisory intent)
  │    INSERT budget_reservations (admission hold, whole chain)
  │    INSERT run_specs (frozen RunSpec JSON)
  │    INSERT outbox + events ('run.queued')
  │  TX COMMIT
  └─ 202 {run_id}                                    ← ~15ms, no graph run

Outbox relay → XADD runs.queued

Worker:
  claim: UPDATE ... fence = fence + 1 → Lease(fence=1)
  load RunSpec from run_specs                        ← never live config
  graph.ainvoke(..., durability=spec.durability)
    node "effect":
      ToolGateway.execute(...)
        lease.check(fence)                           ← StaleFence stops a zombie
        permission: spec.allowed_tools, then the live grant (≤30s cache)
        kill switch: local override, then the table (10s cache)
        authority: the RunSpec's frozen ResolvedAuthority; approval if human
        budget: reserve against the whole chain, one locked statement
        rate limit: token buckets — refusal releases the hold
        key = logical_call_id(run, ns, node, ordinal, args_hash)
        journal.begin(key) → INTENT, own transaction, own connection
        credentials: fetched now, never pinned
        ── execute ──
        lease.check(fence) again
        kill switch again — halt only; drain lets this commit
        scrub secrets, then artifact if > 32KB
        journal.commit(key, result)
        audit; budget reconcile
        ── one batched INSERT of every decision this call made ──
    node "respond"
  finish: UPDATE runs SET status=SUCCESS  ← fence-guarded
  INSERT outbox ('run.succeeded')
```

Kill the worker between `── execute ──` and `journal.commit`. The reaper requeues,
a second worker claims at `fence=2`, the node replays, `logical_call_id` recomputes
identically, the journal answers INTENT, the `probe` policy finds the marker and
commits without re-firing. One row. Every time, on both durability modes.

Everything M2 added sits *before* the journal or *after* the effect, and that
placement is the whole reason the paragraph above still reads the same. A call that is
going to be refused is refused before it can leave an INTENT row; a call that has
already fired is only ever stopped by `halt`, deliberately, leaving a row somebody has
to reconcile. The twelve chaos tests pass unchanged.

---

## 5. What M1 adds

Four mechanisms, and each one is load-bearing for a number rather than for a
feature.

| Mechanism | Lives in | Without it |
|---|---|---|
| Pinned output schemas | `domain/schemas.py`, `domain/outputs.py` | "did it meet the spec" is unanswerable, and a task written Monday can be judged Friday against a schema that moved |
| The outcome state machine | `org/tasks.py`, `org/evaluation.py` | rework runs forever, and `AUTO_ACCEPTED` leaks into the acceptance numerator where it flatters everything |
| `work_class` on every model call | `gateway/models.py`, `graphs/common/structured.py` | the coordination ratio — the whole v3 §17 thesis — has no numerator |
| The metric views | migration `015_metrics` | every §9 decision is made on numbers nobody checked; T26 is the only thing that would catch it |

Three properties are worth stating because they are easy to erode:

*Nothing in `runtime.org` calls a model.* Assignment, notification, closure, the
deadline sweep and the metric reads are rules. That is what licenses §3's
conclusion — if the coordination ratio still comes out high, the hierarchy is the
problem and not the plumbing.

*The evaluation call is isolated.* `marketing_head@1`'s evaluate node builds its
context by hand rather than through the fixed template, because the template would
helpfully add the session summary and the recent messages — which include this same
actor deciding the task was a good idea.

*Graph state holds artifact references, never bodies.* LangGraph checkpoints the
whole state at every superstep, so a report carried in state is re-serialised on
every node and every replay. `docs/M0_RETRO.md` has the measurement;
`test_m1_state_discipline.py` has the assertion.

---

## 6. What M2 adds

Six mechanisms, and unlike M1's they are load-bearing for a *property* rather than
for a number. The number they are measured against is that they did not cost too
much — which is why §9 of the M2 plan makes non-regression an exit criterion and why
`docs/M2_GATE.md` exists.

| Mechanism | Lives in | Without it |
|---|---|---|
| Authority resolver | `org/authority.py`, `domain/authority.py` | "what may this actor do" is a dict with one entry, and the answer for anything unlisted is *yes* |
| Hierarchical budget | `budget/service.py`, `persistence/repositories/budget.py` | one actor's bad week is the organization's whole month, and nothing can promise more than it holds |
| Approvals with a chain | `org/approvals.py` | `on_expiry` fires on the first person who was in a meeting, and one approver absorbs every gate until they stop reading them |
| Kill switch | `org/killswitch.py` | stopping requires a redeploy, which means stopping does not happen |
| Trust fencing + corpus | `domain/trust.py`, `tests/fixtures/injection/` | the `UNTRUSTED` tag is a comment, and "are we exposed to injection" is answered by opinion |
| Decision audit | `persistence/repositories/audit.py` | you know what your agents did and not what they kept trying to do |

Five properties are worth stating because they are the ones most likely to erode.

*The gateway reads the frozen spec, never live policy.* A run executes under one
authority for its whole life and `spec_hash` is the receipt. The single exception is
revocation, which is a ≤30s re-check rather than a mutation — because a spec that
changes under a run makes "what was this allowed to do" unanswerable at the moment
somebody needs the answer.

*There is exactly one reservation path.* Not two, not an optimised one for the common
case. The lock order is `depth ASC, id ASC` and the strength is `FOR NO KEY UPDATE`;
both are load-bearing and neither is obvious, which is why a test asserts the literal
SQL and an import contract keeps the tables private.

*Denials are the valuable rows.* Every gateway decision is recorded, allowed ones
included, because a denial rate without a denominator is a count. They are buffered
and written in one statement per call, because eight checks writing eight rows is how
a governance milestone regresses the number it was told not to regress.

*The approver sees the action, not a summary of it.* `_render_action` builds the
approval's detail from the arguments that will really be sent — scrubbed, and
truncated loudly rather than quietly. A model-written summary is the model's account
of its own intentions, and that is precisely the thing under review.

*Fencing is not the security posture; the corpus is.* `docs/INJECTION_RESULTS.md` is
generated by the test that runs it, and its "known gaps" section is prose because a
gap with a written rationale is a position and a gap in a table is an oversight
waiting to be noticed.

---

## 6a. What M3 adds

Six mechanisms, and M3 is the first milestone that can make the system **worse**.
Governance added overhead but could not degrade output quality; memory can — a wrongly
retrieved fact is worse than no fact, and retrieval costs tokens on every call whether
or not it helped. Everything below is arranged around that asymmetry.

| Mechanism | Lives in | Without it |
|---|---|---|
| `MemoryStore` port + two adapters | `memory/store.py`, `memory/mem0_store.py` | the three zero-tolerance isolation gates are conditional on an optional system package |
| The metadata sidecar | `memory_metadata`, `persistence/repositories/memory.py` | scope isolation is a third-party library's filter, which §13 risk 3 names as a risk |
| `run_trust` and quarantine | `domain/memory.py`, `memory/service.py` | a poisoned page becomes a company-scoped fact that every actor reads forever |
| `context_traces` and shadow mode | migration 029, `memory/planner.py` | quality and cost move together on the day injection goes live and you can attribute neither |
| The promotion queue | `memory/promotion.py`, migration 028 | company memory accumulates whatever the extraction model happened to emit, with nobody accountable |
| The nine evals with a corpus | `memory/evals.py`, `memory/golden.py` | every §11 number is a guess with a decimal point |

Five properties are worth stating because they are the ones most likely to erode.

*Shadow mode is the default, and the prompt is byte-identical.* `PlannedContext.block`
is the empty string unless injection is on, so `assemble(memory=...)` needs no branch
and phase one costs exactly nothing. `test_m3_shadow.py` asserts it as a string
comparison rather than as an argument, and `ck_trace_shadow_injects_nothing` says the
same thing to the database.

*The isolation boundary is ours, not the library's.* Every retrieval is authorised by
one SQL predicate in `MemoryMetadataRepository.authorize`; the store's own scope filter
is a prefilter and a performance hint. A regression in the library costs recall, not
isolation, and T48/T49 pass against either adapter because they test the same statement.

*Nothing writes memory on the request path.* The write path is an outbox consumer on its
own group over `run.succeeded`, so it cannot add latency to a run — T44 asserts the
absence of the call rather than measuring how fast it is.

*There is one door that widens a scope.* `move_scope` has a single caller, and T53
checks that by reading the source, because an absence is the one thing a behavioural
test cannot demonstrate. Two CHECK constraints make an un-reviewed approval and a
two-rung promotion unrepresentable.

*An unmeasured eval is not a passing eval.* Every `EvalResult` carries `measured` and
the provenance of the corpus it came from, and eval 2 refuses to score an unlabelled
`queries.jsonl` rather than returning a number that is about the labelling. §13 risk 5's
failure mode is not an error — it is a plausible figure nobody checked.

---

## 6b. What M4 adds

One mechanism, and it is a control plane rather than a runtime change. M4 is the only
milestone whose claim is *nothing about execution changed*, and that claim is checkable:
compiling `config/org/` produces byte-identical `spec_hash` values to
`runtime.org.department`, asserted on every CI run.

| Mechanism | Lives in | Without it |
|---|---|---|
| Documents → `ActorSpec`, by value | `spec/documents.py`, `spec/compile.py` | a parallel "compiled YAML spec" type that has to be *argued* equivalent rather than *being* equal |
| The §6 semantic checks | `spec/validation.py` | a dangling reference is a `KeyError` in a worker three days later; a cyclic policy is two runs waiting on each other |
| Two-phase, one-transaction apply | `spec/apply.py` | a half-created organization whose applied half looks deliberate |
| `plan` / `plan_hash` / staleness | `spec/differ.py`, migration 032 | applying a diff nobody reviewed, because somebody else applied something in between |
| The exporter | `spec/exporter.py` | hand-written YAML, and two things to debug instead of one |
| `spec drift` | `spec/drift.py` | the database and the files diverge and nobody finds out |

Four properties are worth stating because they are the ones most likely to erode.

*The compilation target is `ActorSpec` itself.* Not a new type that resembles it. That
single decision is what turns §8's equivalence claim from an argument into
`assert compiled.spec == HEAD_SPEC`, and it is what makes the round-trip test cheap
enough to keep forever.

*Every reference column is deferred to phase 2*, not only the department/head cycle that
motivated two-phase apply. Same mechanism, better errors: a reporting loop then reaches
the validator, which names the two documents, instead of falling out of a topological
sort as a list of everything that transitively depended on them.

*`apply` publishes a new actor version only when the hash moved* — deliberately unlike
`seed_department`, whose unconditional republish is correct for a boot and wrong for a
control plane. If every apply churned a version for every actor, "nothing changed" and
"everything changed" would be indistinguishable in `actor_versions`, which is exactly
where somebody would look to check.

*It is not a reconciler, and the omission is load-bearing.* `apply` is a command, `drift`
reports and never heals, and there is no `--fix`. The next feature after `--fix` is
"apply on git push", and the one after that is an organization that rewrites itself
unattended in the middle of a measurement window.

---

## 6c. What M5a adds

One mechanism — a run can create a run — and six things that stand between it and the
ways that goes wrong. Like M3 it ships dark: `RUNTIME_DELEGATION_ENABLED` is false, and
a worker built with it false has no `DelegationService` at all, so no code path inside
it can create a child.

| Mechanism | Lives in | Without it |
|---|---|---|
| Child runs through the one door | `runtime/delegation.py` → `RunService.start_run` | a second creation path, and eight admission checks that apply to one of them |
| `ChildContext` | `domain/delegation.py` | the parent's message history in the child's prompt, which is both the token cost and the blast radius |
| The eight checks | `run_service.py`, `domain/delegation.py` | a cycle, a fan-out, or a child wider than its parent |
| The root-run budget pool | `budget/service.py` (built in M2, on in M5) | one badly decomposed parent spends its actor's whole month in an afternoon |
| Cascade cancel / late attach | `worker/executor.py`, `delegations` table | orphans spending a dead tree's money, and results nobody kept |
| `departments` | migration 034 | a department is a string, and two of them have no referential integrity |

Four properties, and the first two are the ones that will erode.

*A child receives a task, extracted facts, a scope subset, a permission subset, budget
headroom and a deadline — and **never the parent's message history**.* This is stated
as a type with `extra="forbid"` rather than as a rule, because the parent's history is
one attribute away at every call site and nothing but a type stops it going in. T60
asserts over the field set, so adding one is a test failure rather than a review.

*The child's idempotency key is a deterministic function of (parent run, node,
checkpoint namespace, ordinal, target).* Spawning is a side effect and a replayed node
re-executes its body; a `uuid4()` here produces a second child on every crash, and the
symptom is a subtree that cost twice what the ledger predicted, days later, with nothing
pointing at the line responsible. Same discipline as `logical_call_id`, same reason.

*`agent_path` is derived inside `start_run`, never supplied.* A caller that could hand
in a path would hand in one that omits the actor it is about to spawn, and the cycle
check would pass on a cycle.

*The parent waits by polling while holding its lease.* Not a suspend and resume — that
would be a new run lifecycle with its own failure modes, shipped dark and exercised by
nobody for a fortnight. Polling makes a worker crash the ordinary lease-expiry case the
reaper has handled since M0. It costs a worker slot for the child's duration, which is
the right trade at depth 2 and the wrong one at depth 5 — and depth > 2 is out of scope.

**M5b, the second department, is a YAML file and an `apply`, and it has not been done.**
It is gated on M1's gate numbers, because doubling the organization before you know what
one department produces means the two effects can never be separated again.

---

## 7. Deliberate deviations from the v3 plan

| Deviation | Reason |
|---|---|
| Budgets ship in M0, not M2 | I8 is an invariant. Retrofitting reservation points into every gateway call path later means touching every call site under concurrency. M2 adds allocations and priority admission on top of a mechanism that already holds. |
| `LeaseManager.check()` is `async` | It is a database read. A local comparison against a cached fence is exactly what a frozen worker has stale. |
| `audit_log` lands in migration `004` | The tool gateway writes an audit row in the same pipeline that writes the effect intent. Shipping them separately would leave a schema version where the gateway cannot run. |
| `scripts/devstack.sh` alongside Docker Compose | Compose is the supported path. The script is the fallback for a machine without Docker; it initdb's a private cluster under `.devstack/` with no sudo and no system services touched. |
| M1 metric views are plain, not materialized | A materialized view needs a refresh job, and a refresh job can be stale on the morning somebody reads the dashboard and makes a decision. At M1 volumes a live view costs nothing. |
| M1 ships four cron triggers, not two | §2's scope names the Monday plan and the Friday summary; §3's loop also needs the Thursday gate and the Friday metrics. Same machinery either way; four means the gate and the metrics run on the same dedupe-and-catch-up path as everything else rather than being hand-invoked steps nobody measures. |
| The session summarizer is a shared graph node, not a fifth actor | A fifth actor would need its own spec and runs, would open a window in which the next run reads a stale summary, and would make the coordination ratio measure a department that is not the one under test. |
| `work_class` gains four coarse values | §7's ratio is defined over WORK / COORDINATION / EVALUATION / SUMMARIZATION. M0's finer-grained values remain legal but land in `overhead_ratio`, so a mis-tagged call makes the number worse rather than better. |
| M2 adds `approvals.ttl_seconds`, not in §2's migration sketch | Escalation rewrites `expires_at`, so the obvious derivation — `expires_at - created_at` — measures how late the *previous* approver was rather than how long the next one has. T33 caught it. |
| The chain lock is `FOR NO KEY UPDATE`, not `FOR UPDATE` | `budget_pools.parent_id` is a self-referencing FK, so creating a child pool takes `FOR KEY SHARE` on its parent and holds it. `FOR UPDATE` conflicts with that, and two admissions under one root deadlock — which T27 found on the first run. Nothing here mutates a key, so the weaker lock is the honest one. **M5 gives `runs` the same shape** through `runs.parent_run_id`, so `lock_for_fanout` takes the same weaker lock, and the source scan that guards the rule now covers both files. |
| `ensure_pool` uses unqualified `ON CONFLICT DO NOTHING` | It named `uq_budget_pool_scope` until M5. But a pool's `id` is a `uuid5` of the same scope, so two concurrent inserts of a *new* pool collide on the primary key first — and a clause naming one constraint does not cover a violation of another. The insert raised `UniqueViolation` and took the admission with it. Invisible until M5 made pool *creation* the common case (one per run tree); T55 found it on the first run. Safe unqualified precisely because the id is derived: there is no second way to conflict. |
| The fan-out check locks the parent run row | Checks 3 and 4 were a read-then-write race, and the argument that dismissed it — a parent is one run on one worker — was true and irrelevant. The concurrency is *inside the node*: a parent fanning out with `asyncio.gather` runs three admissions from one lease, all three read zero live children, all three are admitted against a limit of two. T57 caught it, and had passed with the check deleted. |
| `RunRepository.finish` is guarded by status as well as fence | The fence answers *do you still own this run*; the status answers *is it still yours to end*. A cascade cancel writes CANCELLED onto a live descendant whose worker keeps its lease and fence, and the fence alone would let that worker overwrite it with SUCCESS a second later — the subtree ceiling enforced in the ledger but not in the run history. |
| `budget_pools.allocated_cents` is denormalised | An ancestor's live allocations are those of its whole subtree, so the honest query is a recursive descent per level on every admission. Two methods maintain the counter, both under the chain lock, and `reconcile_allocated_cents` repairs it. |
| M2 ships a `GovernanceSweeper` that §2 does not mention | `Settings` has carried `budget_sweep_interval_seconds` since M0 and `approval_sweep_interval_seconds` since M1, and nothing ran either outside the CLI and the tests. Survivable while the only time-driven behaviour was a TTL nobody waited on; not survivable once escalation is on a clock. |
| A department policy does not govern its own approver | Read the other way, the shipped configuration fails the acyclicity check for a cycle it does not contain, and a director's approval would route to the director. `_subject_roles` and `_governs` are the two halves that agree. |
| **M3:** `MemoryStore` is a port with two adapters, and Mem0 is not the default | pgvector is absent from stock `postgres:16-alpine` and from `scripts/devstack.sh`'s cluster. M3's three zero-tolerance isolation gates would otherwise be conditional on an optional system package, which is how a hard gate becomes decorative. `NativeMemoryStore` does exact cosine over `real[]` in one SQL statement — correct at M3 volumes, with no ANN recall parameter to confound eval 1. `RUNTIME_MEMORY_STORE=mem0` is the production path and never falls back silently. |
| **M3:** the memory scope filter is ours, not the library's | v3's own risk list names "scope filtering is a security boundary implemented by a third-party library" as a risk. Making the authoritative predicate ours removes it: a library regression costs recall, not isolation. The store's filter is kept as a prefilter, and T48/T49 pass against either adapter because they exercise the same SQL. |
| **M3:** migration `030` widens `audit_logs.gateway` | M2 made the gateway vocabulary a closed CHECK, which was right. M3 adds a fourth gateway and the constraint fired on the first test run. Widening it deliberately beats the shortcut of not auditing embeddings — which are the highest-volume external calls M3 makes, and the last ones that should be missing from the denial stream. |
| **M3:** `run_trust` is computed from provenance, not from fencing | The plan says "if any untrusted content was in the run's context". Since M2 fences *every* inbox message unconditionally, the literal reading quarantines every run in the department forever and the rule returns one answer. Trust is therefore computed from the effect journal plus inherited taint across a `correlation_id` chain. Both error directions, and the one real gap (content arriving through a run's *input*), are written down in `docs/M3_SHADOW.md` §6. |
| **M3:** memory extraction runs on the pro model, in a background worker | The plan's own risk list: "the extraction model determines memory quality permanently... this is the wrong place to economize." Every other cheap-model decision here is reversible by re-running the call; this one is not, because the bad fact is already in the store being retrieved. `work_class=MEMORY` on the dashboard from day one is the counterweight. |
| The budget's fourth level — root run — is built but off | §1 names `org → department → actor → root run`. Delegation is out of M2's scope, so a run tree has one member and the pool would bound a single run that `ceilings.max_cost_cents` already bounds — at the price of a row per run and a lock per reservation, which is the per-call cost §9 is watching for. `ensure_chain(root_run_id=…)` enables it and a test exercises it, so M5 changes a parameter rather than adding a level. |

---

## 8. Not in M2 — and what M3 took off the list

```
memory · Mem0 · pgvector · the Context Engine · delegation · retrieval ·
YAML control plane · a second department · semantic cache · multi-tenancy ·
HYBRID actors · model routing tiers
```

**M3 built the first five**, and one of them differently than the list implies:
`pgvector` is optional rather than required, because it is absent from stock Postgres
and gating M3's three zero-tolerance isolation evals on a system package would have made
them decorative. See §7's deviation table and `docs/M3_LIBRARY_FACTS.md` fact 3.

Still not built, and now M5's list: graph memory, semantic cache, delegation, a second
department, `HYBRID`, cross-department sharing beyond `COMPANY`, and Mem0 Cloud.

**M4 built the config plane** and deliberately stopped short of a reconciler: no
`--fix`, no daemon, no apply-on-push. The org is now easy to change, which is a reason
to be more careful about *when*, not less — §12's operational rule is that no `apply`
runs during a measurement window, and nothing in the code enforces it.

Three things left the list in M2 and are worth naming: the **full authority model**,
**approval escalation** and the **kill switch** were all "not in M1" and are now
built. `approvals.escalation_count`, added in migration 014 and never incremented,
is incremented now — which is exactly the retrofit that column was created to avoid.

`tasks.input_schema_ref` still exists and is still never set. Typed chaining between
tasks was pencilled in for M2 and is not part of §1's scope; the column stays so
adding it remains a backfill rather than a migration on a hot table.

Two M2 items are deliberately *deferred to the gate* rather than built: **model
routing by `work_class`** and **artifact summary compression**. §0 names both as cost
levers, and the mapping for the first already exists — M1 runs DeepSeek pro for work
and flash for summarisation. Widening either is a response to §9's measurement, not a
deliverable ahead of it, because tuning cost before measuring it is how a milestone
optimises the wrong thing convincingly.

If M2's non-regression week fails on cost, §9 names three suspects and this codebase
has a guard and a test-of-the-guard for each. `docs/M2_GATE.md` is the worksheet.

**M3's exit criterion is not "memory works".** It is that memory *pays for itself*: one
week with injection on, rejection rate improved or unchanged with a stated reason, cost
per accepted outcome within +15% of M2's and P95 latency within +20%. If rejection rate
does not improve and cost is up, memory has not earned its place — the honest outcome is
to keep it in shadow mode, keep traces accumulating, and move to M4. §6a below is what
M3 adds; `TODO_M3.md` tracks it, `docs/M3_SHADOW.md` holds the bars set in advance, and
`docs/M3_LIBRARY_FACTS.md` is the §3 verification with the version and date on it.

`ActorKind.HYBRID` exists in the enum and raises `NotImplementedError` at spec
compile. When a real case arrives it ships with a mandatory
`allowed_model_call_sites: frozenset[str]`, and `ModelGateway` rejects any call
whose `call_site` is not declared. Not building it is a stronger guarantee than
policing it.
