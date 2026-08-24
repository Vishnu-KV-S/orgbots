# M5 TODO — delegation, and the second department

M5 is two milestones that earlier drafts treated as one, and the split is the whole
point.

| | What it is | Status |
|---|---|---|
| **M5a — Delegation** | Code. Child runs, subtree budgets, cascade cancel, isolation | **Built, green, and off by default** |
| **M5b — Second department** | A YAML file and an `apply` | **Not started. Gated on M1 gate numbers** |

**M5a status: 941 tests pass** — 96 of them M5's — with ruff, ruff format, mypy strict
and all five import-linter contracts clean. M0's T0–T13, M1's T14–T26, M2's T27–T43,
M3's T44–T54 and M4's 156 pass unchanged.

`RUNTIME_DELEGATION_ENABLED` defaults to `false`, and T68 asserts the strong form of
what that means: a worker built with the flag off has **no delegation service at all**,
so no code path inside it can create a child run. M4 behaviour is the behaviour by
construction rather than by a flag being checked in the right places.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## §2 — M5a scope

- [x] `departments` table and the FK migration deferred from M4 (034)
- [x] Child run spawning through `RunService.start_run()` — no second path (I1)
- [x] `agent_path` cycle prevention, **derived rather than supplied**
- [x] Subtree ceilings: `max_subtree_cost_cents`, `max_subtree_llm_calls`,
      `max_children`, `max_live_descendants`, `max_delegation_depth`
- [x] Root budget pool activation (chain depth 3 → 4)
- [x] Privilege subset enforcement at admission
- [x] Cascade cancel on parent terminal
- [x] Late-child handling: persist, attach, event, never resume
- [x] Child context isolation
- [x] Parent wakeup on child completion — `[~]` by polling, see the deviations
- [x] `drain` / `strict` exhaustion policy
- [x] Delegation off by default: `RUNTIME_DELEGATION_ENABLED=false`

**Out, and stayed out:** temporary subagents, delegation depth > 2 (enforced by
`spec.validation.MAX_DELEGATION_DEPTH`, not merely documented), cross-department
delegation, M6 hardening.

## §3 — the departments table

- [x] `034_departments`, in §3's order: create, backfill, `actors.department_id`,
      re-point, keep the text column
- [x] **The re-point moves no rows.** `departments.id` *is*
      `budget.department_scope_id(org, name)` and `departments.memory_scope_id` *is*
      `org.department.department_scope_id(org, name)`, so the foreign key arrives over
      data that already agreed
- [x] A trigger keeps `department` and `department_id` in sync **in both directions**
- [x] Tested against a database with M3 memories in it, not an empty one —
      `test_034_backfills_over_live_m3_memories` downgrades to 033, seeds a live M3
      department, and upgrades
- [x] A pinned RunSpec still says what it said: `test_a_pinned_run_spec_still_resolves_after_034`
- [~] `department_id` is **nullable**, not `NOT NULL`. See the deviations.

## §4 — the eight admission checks

All eight at `start_run()`, in §4's order, and the ordering is enforced by where they
sit in the method rather than by a comment.

| # | Check | Raises | Where |
|---|---|---|---|
| 1 | target ∉ `agent_path` | `DelegationCycle` | `extend_agent_path` |
| 2 | `depth + 1 ≤ max_delegation_depth` | `DepthExceeded` | `check_depth` |
| 3 | live children < `max_children` | `FanoutExceeded` | `RunService._check_fanout` |
| 4 | live descendants < `max_live_descendants` | `FanoutExceeded` | same |
| 5 | child authority ⊆ parent | `PrivilegeEscalation` | `check_authority_subset` |
| 6 | child scopes ⊆ parent | `ScopeEscalation` | `check_scope_subset` |
| 7 | subtree cost + estimate ≤ ceiling | `SubtreeBudgetExceeded` | `_check_subtree_ceilings` |
| 8 | subtree calls < ceiling | `SubtreeCallsExceeded` | same |

- [x] 5 and 6 run **before** 7 and 8, which touch the row the reservation is about to
      lock
- [x] `agent_path` is derived inside `start_run` by appending the child's own actor
      name to the parent's — a caller cannot hand in a path that omits the actor it is
      about to spawn, which would defeat check 1 while looking well-formed
- [x] `ChildContext` is the isolation claim as a type: task, facts, scopes, headroom,
      deadline, and `extra="forbid"`

## §5 — the depth-4 deadlock retest

- [x] T55 at 200-way concurrency, **both pool configurations**, zero deadlocks, zero
      CHECK violations, exact arithmetic at all four levels
- [x] A second shape §5 asks for specifically: pool **creation** racing reservation,
      rather than pool update alone
- [x] M2's chain-lock source scan extended to `runs.py`, which M5 gives the same
      `FOR KEY SHARE` hazard through `runs.parent_run_id`

**T55 found two real bugs**, both pre-existing and neither reachable before M5:

1. **`ensure_pool` named the wrong constraint.** `ON CONFLICT ON CONSTRAINT
   uq_budget_pool_scope DO NOTHING` does not cover a conflict on `budget_pools_pkey` —
   and `id` is a `uuid5` of the same scope, so two concurrent admissions for a
   *new* pool collided on the primary key and raised `UniqueViolation`, taking the
   admission transaction with them. M2 never saw it because T27 pre-built every chain
   before hammering it, so the only concurrency was on `UPDATE`. M5 makes pool creation
   the common case — one pool per run tree — and T55's second half found it on the
   first run. Fixed to unqualified `ON CONFLICT DO NOTHING`, which is safe precisely
   because the id is derived: there is no second way to conflict.

2. **The fan-out check was a read-then-write race**, and the reasoning that dismissed
   it was wrong in a way worth recording. The first version argued that a parent is one
   run on one worker, so two simultaneous delegations would need two holders of one
   lease. True, and irrelevant: the concurrency is *inside the node*. A parent that
   fans out with `asyncio.gather` runs three admissions from one lease, all three read
   zero live children, and all three are admitted against a limit of two. T57 caught it
   on the first run. Fixed with `RunRepository.lock_for_fanout` — `FOR NO KEY UPDATE`
   on the parent's own row, taken before the budget chain so the lock order stays
   total.

## §6 — tests

| # | Test | Where |
|---|---|---|
| T55 | Depth-4 chain reservation, both pool configs | `test_m5_chain.py` (5) |
| T56 | A→B→A refused at admission | `test_a_cycle_is_refused_at_any_length`, `..._delegating_back_to_an_ancestor...` |
| T57 | Fan-out at max children / descendants | `test_too_many_children_is_refused_and_the_parent_continues` (+2) |
| T58 | Parent terminal → descendants cancelled `PARENT_TERMINAL` | `test_a_terminal_parent_cancels_every_live_descendant` (+1) |
| T59 | Late child → persisted, attached, event, no resume | `test_a_child_that_finishes_after_its_parent_is_persisted_not_resumed` (+1) |
| T60 | **Child context has no parent history** | `test_a_child_context_cannot_carry_parent_history` (+1 end-to-end) |
| T61 | Child authority ⊃ parent → refused | `test_a_child_with_wider_authority_is_refused` (+1 unit) |
| T62 | Child scope ⊃ parent → refused | `test_a_child_with_wider_memory_scopes_is_refused` (+1 unit) |
| T63 | Cost ceiling → `drain` | `test_the_subtree_cost_ceiling_drains` |
| T64 | Cost ceiling → `strict` | `test_the_subtree_cost_ceiling_can_cancel_instead` |
| T65 | Call ceiling without cost ceiling | `test_the_call_ceiling_binds_without_the_cost_ceiling` |
| T66 | Worker killed mid-child → parent resumes, child not duplicated | `test_a_reclaimed_parent_finds_the_child_it_already_spawned` |
| T67 | Spawn idempotent under replay | `test_a_replayed_spawn_reuses_the_same_child` (+1 unit) |
| T68 | Delegation off → refused, byte-identical to M4 | `test_delegation_off_refuses_and_builds_no_service` (+3) |
| T69 | Hop limit still holds with delegation active | `test_the_message_hop_limit_still_holds_with_delegation_active` |
| T70 | Injection corpus re-run with delegation on | `test_m5_injection.py` (50) |

**T60 does not check that a particular call passed no history; it checks that there is
nowhere to put any.** The assertion is over `ChildContext.model_fields`, so adding a
`messages` or `parent_summary` field is a test failure rather than a review somebody
might wave through. §9 risk 5 predicts that pressure by name.

**T70 reuses M2's corpus unchanged** — the same twenty payloads from the same manifest —
because a corpus rewritten for the feature under test is a corpus that tests the feature
it was written for. What is new is the ingress: delegation is a fifth one, and the thing
crossing it is not content but a request to create a run.

## §7 — M5a exit criteria

**Hard:**

- [x] T55–T70 green
- [x] T68 proves a checkout with M5a merged and delegation off is indistinguishable
      from M4 — including `runs.agent_path = '{}'` and `RunSpec.delegation = null`
- [x] Migration 034 tested against a database containing M3 memories
- [x] M0's chaos suite unchanged; M2's chain-lock test green in both pool
      configurations

**No live measurement required**, because it ships dark. That is the point of the split.

## §8 — M5b

**Not started, and deliberately.** §9 risk 6: *"M5b before the gate makes every
downstream number uninterpretable. This is the last milestone where holding the line is
cheap."*

`config/org/` is **untouched by M5a**. No actor gained a `delegation:` block, no second
department exists, and `spec plan` against the committed corpus is empty. That is not an
oversight — turning delegation on for the M1 department is a config change, and M4's
operational rule says not to make one during a measurement window.

When the gate numbers exist, M5b is:

1. Write `config/org/departments/<name>.yaml` and its actors
2. Update the single `BudgetPolicy` document
3. `spec plan`, review the diff, `spec apply`
4. Run one week

`[CHOSEN]` exit, unchanged from the plan: coordination ratio within +5pp, cost per
accepted outcome within +15%, rejection rate no worse, cross-department messages
measured.

**And delegation must pay for itself**, on the same terms memory did: compare a
delegated task against the same task done directly. If the subtree does not cost less
and the output is not accepted at a higher rate, keep it off. The code is not wasted.

---

## Deliberate deviations

| Deviation | Reason |
|---|---|
| **The parent waits by polling**, holding its lease and heartbeating, rather than suspending into `WAITING_CHILD` and being resumed by an event. | §2 lists "parent wakeup on child completion" and `WAITING_CHILD`, and both exist — the status is real and the parent does wake — but the wakeup is a poll rather than a resume. A suspend/resume path is a new run lifecycle with its own failure modes, and it would ship dark and be exercised by nobody for a fortnight. A polled wait makes a worker crash *the ordinary lease-expiry case the reaper has handled since M0*, which is why T66 is four lines. The cost is a worker slot held for the child's duration: right at depth 2, wrong at depth 5, and one more reason depth > 2 is out of scope. |
| **`WAITING_CHILD` did not exist.** §1 lists it, `root_run_id`/`parent_run_id` and the privilege-subset check as things M2 already built. Two of the three were there; the status was not. | Added in M5 as a non-terminal, lease-held status, with `claim`, `heartbeat`, `reap_expired` and `claimable` widened to name it. Behaviour-neutral with delegation off, because no run ever has that status. It is worth having for one reason and it is diagnostic: a department with delegation on will have runs whose wall clock is dominated by waiting, and a dashboard that could not separate "thinking" from "waiting for someone else to think" would make the overhead M5b has to measure invisible. |
| **`agent_path` is on `RunSpec`, and the column is the audit copy.** §1 implies a column from migration 002; there was none. | The cycle check runs against the frozen spec, because a run executes under its spec and a path read from `runs` would be a second source of truth for something the spec already knows. The column exists so A→B→A is answerable in SQL after the fact, which turns "the guard held" from a claim into something an operator can verify. |
| **`DelegationLimits` lives on the `actors` row, not in `ActorSpec`.** | Adding a field to `ActorSpec` changes every `spec_hash` in the department and fails M4 §8's round-trip — to describe a limit the runtime enforces at admission anyway. Exactly the argument M4 made for keeping `MemoryProfile` out, and the same route `RunSpec.memory_scopes` takes. The four M1 `spec_hash` values are still `cc9f53c2…`, `e0bc8784…`, `2d83f90a…`, `702aeae0…`. |
| **`actors.department_id` is nullable.** §3 step 3 says "then NOT NULL". | An actor with no department is not a degenerate state to migrate away from: it is the case `BudgetService.ensure_chain` is written around — *"forcing a synthetic pool in between would put a level in the lock chain that means nothing."* `NOT NULL` would require inventing a department for every unplaced actor, which is a worse lie than a null. |
| **`departments` carries two scope ids** rather than unifying them. | M2 derived a department's budget scope from `department:{org}:{name}` and M3 derived its memory scope from `memscope:department:{org}:{name}`. Unifying them here would mean rewriting either every `budget_pools.scope_id` or every `memory_metadata.scope_id` under a live organization — the blast radius M4 declined and §3 says to avoid. Carrying both is honest; the migration mirrors both derivations and a test asserts the copies agree. |
| **The migration re-implements the two `uuid5` derivations instead of importing them.** | A migration that imports application code runs whatever that code says *today*. A derivation that quietly changed would silently re-key every department in every database the migration had already run against. `test_the_migration_mirrors_the_two_scope_derivations` loads the migration by path and compares. |
| **`POST /v1/runs` now refuses `parent_run_id` with a 400.** | Until M5 that field created a child run with none of the checks a child needs, because those checks did not exist. They do now and they live behind `delegate()`; an HTTP caller that could reach around them would be the one hole in §4's list. Refused rather than ignored: a parameter that stops meaning what it said should say so. No test covered it, and none does now beyond the refusal. |
| **`RunRepository.finish` gained a status predicate** alongside the fence. | The fence answers *do you still own this run*; the status answers *is it still yours to end*. A cascade writes CANCELLED onto a live descendant whose worker keeps its lease and its fence, and the fence check alone would let that worker come back a second later and overwrite CANCELLED with SUCCESS — the subtree ceiling enforced in the ledger and not in the run history, which is the worst of both. |
| **`delegator@1` has a concurrent mode as well as a sequential one.** | Sequential is the reference and the safe default. It is also the shape in which the *live* fan-out limits can never bind, because there is never more than one live child — T57's first version passed with check 3 deleted. A parent that genuinely decomposes work wants its children running together, and that is the parent those limits exist for. The concurrent path passes the loop index as the ordinal explicitly, because `gather`'s start order is an implementation detail and an ordinal drawn from a shared counter would make the idempotency key a function of the scheduler. |
| **The subtree ceiling is the *parent's* limit, carried on the request.** | A subtree ceiling belongs to whoever opened the subtree. The child's own limits govern what *it* may delegate one level further down, and are resolved from its own actor row like any other run's. `StartRunRequest.parent_limits` is how the parent's frozen copy gets there — frozen, so a limit raised mid-run cannot retroactively admit a child. |
| **`enforce_exhaustion` applies the policy to the subtree and reports; it never ends the parent.** | A service that killed the run its caller was executing would be a control-flow surprise in the middle of a graph node. `strict` cancels the descendants and sets `limit_reached` in the parent's output; the graph decides its own ending. T64 asserts the cancellation, not the parent's status. |
| **`config/org/` is unchanged.** | See §8. Turning delegation on for the M1 department is a config change and M4's operational rule says not to make one during a measurement window. The `delegation:` block is validated, storable and enforced; nothing in the committed corpus uses it. |

---

## What M5a deliberately did not build

- **A resume path.** See the first deviation.
- **Temporary subagents** — a child that is not a registered actor. Every child here
  resolves through `actors`, holds a version, and has a `spec_hash`. An unregistered
  child would be a run nobody could explain six weeks later.
- **Cross-department delegation.** Departments talk by message, and the hop limit is
  what bounds that (T69). Delegation across a department boundary would need a second
  answer to "whose budget" and M5b has not measured the first one yet.
- **A retry or a re-plan when a child fails.** The parent gets a `DelegationOutcome`
  saying what happened and decides. A service that retried on the parent's behalf would
  spend the subtree's budget on a decision nobody made.
