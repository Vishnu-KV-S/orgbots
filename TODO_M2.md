# M2 TODO — governance

Goal: make the runtime safe to point at things that matter — real external effects,
real budgets, real permissions — **without regressing what M1 proved**.

**Status: the machine is built and green. The non-regression week has not been run.**
532 tests pass (520 fast + 12 chaos) in 3m16s; ruff, ruff format, mypy strict and all
five import-linter contracts clean. M0's T0–T13 and M1's T14–T26 still pass unchanged,
including T7 at 50 iterations × 2 durability modes.

M2 is the first milestone whose exit criteria the test suite cannot produce.
`docs/M2_GATE.md` is the worksheet for the part that needs a week of real operation.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## §0 — gate check

- [ ] **M1's numbers recorded as the M2 baseline.** Not done here, and not doable
      here: it is a measurement of M1's last clean weeks, and this milestone has no
      access to them. `docs/M2_GATE.md` §0 is the table to fill in.
- [ ] Confirm no M1 §9 stop condition is still true
- [ ] Note which metric was marginal, so §9's comparison knows where to look

## §2 — migrations 016–022

- [x] `016_authority` — roles, authority_policies, tool_grants, connections;
      `actors.role_name` / `actors.department`
- [x] `017_budget_tree` — `budget_pools.parent_id` / `depth`, `budget_allocations`,
      priority; `ck_budget_root` and `ck_budget_depth` so a hand-written INSERT
      cannot corrupt the lock order
- [x] `018_approvals_v2` — escalation chain, `approver_budgets`, `resume_token`,
      `deferred_until`, and `ttl_seconds` frozen onto the row
- [x] `019_killswitch` — `kill_switches`, one live switch per scope by partial index
- [x] `020_audit` — `audit_logs` partitioned by month, `ensure_audit_partition()`,
      `v_denial_stream`, `v_audit_partition_health`
- [x] `021_ratelimits` — `rate_limit_policies`, per-policy `fail_open`
- [x] `022_credentials` — AES-256-GCM ciphertext, versioned, one ACTIVE row by
      partial unique index
- [x] Every one reversible; `alembic downgrade base && upgrade head` clean (T0)
- [~] **`ttl_seconds` on `approvals` was not in §2's sketch.** Escalation rewrites
      `expires_at`, so deriving the next deadline from `expires_at - created_at` —
      the obvious shortcut, and what the first implementation did — measures how late
      the *previous* approver was. T33 caught it.

## §3 — authority resolver

- [x] Resolution chain: role base → role policy → department → actor → blast-radius
      floor, in `org/authority.py::build_authority`
- [x] Frozen into `CompiledSpec.authority`, therefore hashed into `spec_hash`
- [x] **Default-deny.** M1's resolver defaulted unlisted actions to AUTO and said in
      a comment that this was wrong for M2. Inverted.
- [x] Gateway reads the frozen spec, never live config (I11)
- [x] Revocation caught by the ≤30s permission cache (edge case 64) — T36, and the
      complementary test that the *window is real* and documented
- [x] **Escalation acyclicity** checked at spec-apply and at admission — T32, six cases
- [x] **Delegation subset** written now, dormant until M5 — three cases
- [~] **A department policy does not govern its own approver.** Read the other way,
      the shipped configuration fails T32 for a cycle it does not contain, and
      `build_authority` would route a director's approval to the director. The check
      and the resolver agree; `_subject_roles` and `_governs` are the two halves.
- [~] **`apply_floor` degrades to DENIED, not HUMAN, when there is no approver.** A
      policy that said `auto` named nobody, so raising it to `human` would construct
      an approval nobody can answer. Stricter than the floor asked for, so it does not
      violate "may only tighten", and it shows up in the denial stream as a policy
      that needs an approver.

## §4 — hierarchical budget

- [x] org → department → actor, three levels; an actor with no department hangs off
      the org at depth 1
- [~] **The fourth level — root run — is built and off by default.** §1 names the
      hierarchy as `org → department → actor → root run`. `ensure_chain(root_run_id=…)`
      creates it and a test exercises it end to end, including that it binds. It is not
      enabled because delegation is out of scope: until a run tree has more than one
      member, the pool bounds a single run that `ceilings.max_cost_cents` already
      bounds, while adding a row per run and a lock per reservation — which is exactly
      the per-call cost §9 is watching for. M5 turns it on with a parameter.
- [x] **One** reservation path: `BudgetRepository.reserve_chain`, locking
      `ORDER BY depth ASC, id ASC`
- [x] Every settle path — reconcile, release, sweep, allocation open/close — takes the
      same lock in the same order. An unordered reconcile deadlocks against an ordered
      reserve, and the bug looks like a reservation bug.
- [x] Admission: hard `committed + reserved ≤ limit` and soft
      `+ Σ live allocations ≤ limit × 1.3`, at every level — T29, T31
- [x] Priority degradation below 15% then 5% headroom; in-flight reservations never
      revoked — T30
- [x] Refusal creates the run in `LIMIT_REACHED` with a reason and emits
      `run.refused` — T29 and the admission test through the real `start_run()`
- [x] Reservation sweeper — T28, and now actually *running* (see the caveat below)
- [x] T27: 200 concurrent reservations across overlapping chains, zero deadlocks
- [x] `budget-tables-are-private` import contract, plus a source scan for raw SQL,
      because a direct `session.execute(text("UPDATE budget_pools…"))` needs no import
- [~] **`FOR NO KEY UPDATE`, not `FOR UPDATE`.** T27 found a real deadlock on the
      first run: `budget_pools.parent_id` is a self-FK, so inserting a child takes
      `FOR KEY SHARE` on its parent and holds it for the transaction — which
      `FOR UPDATE` conflicts with. Two admissions creating pools under one root
      deadlock. Nothing here mutates a key, so the weaker lock is the honest one.
- [~] **`allocated_cents` is denormalised.** An ancestor's live allocations are those
      of every descendant, so the honest query is a recursive descent per level on
      every admission. Two methods maintain the counter, both under the chain lock,
      and `reconcile_allocated_cents` repairs it.

## §5 — approvals

- [x] Escalation chain frozen onto the row from the run's authority — T33
- [x] Sweep escalates *before* expiring: chain left means ignored, not expired
- [x] `on_expiry` ∈ `deny | grant | escalate | fail_run`; `fail_run` closes the task
      REJECTED/CANCELLED and emits an event
- [x] Per-approver daily budget: alerts and **defers**, never lengthens the queue —
      T35. Charged per *assignment*, so a replayed gate node does not eat a person's day.
- [x] `resume_token` random, single-use, bound to an interrupt — T34, four refusals
- [x] First decision wins, second recorded (M1's T23, unchanged)
- [x] Decisions on a terminal subject refused (I6, edge case 27)
- [x] **The approver sees the rendered action** — `_render_action`, built from the real
      arguments, scrubbed, truncated *loudly* at 4,000 chars. Never a model summary.
- [~] **Absence is not terminality.** A subject id naming no task is not treated as
      terminal: it is either a caller using task-shaped ids for something else or a
      hard delete, and refusing on ignorance would gate decisions we cannot see.

## §6 — trust boundary and the injection corpus

- [x] `domain/trust.py`: content-derived nonce so the fence cannot be closed from
      inside; control tags escaped rather than deleted; deterministic for replay
- [x] `TRUST_SYSTEM_RULE` on every prompt that carries untrusted material
- [x] Four ingresses fenced: web fetch, inbox, artifact, **task input fields**
- [x] 20 payloads in `tests/fixtures/injection/`, one benign control
- [x] T42: 20 × 4 containment + 19 enforcement attempts against a *fully compromised*
      model. 114 assertions.
- [x] `docs/INJECTION_RESULTS.md` **generated** by the test, with written rationale
      for the known gaps
- [~] **`fence_trust=False` exists for one caller** — the evaluation node, which has
      no untrusted material. Passing untrusted blocks with it raises, because that
      combination is always a mistake.

## §7 — kill switch and rate limits

- [x] `drain` default, `halt` deliberate; the difference is the post-effect check —
      T40, T41
- [x] Four scopes: org, tool, actor, connection; `halt` wins over `drain`
- [x] 10s TTL cache (edge case 70), invalidated on engage so the operator does not wait
- [x] M0's process-local switch kept and checked *first*, for a worker drained by hand
- [x] Redis token buckets, one Lua script, atomic under concurrency
- [x] All three scopes; per-policy `fail_open` with both answers defended
- [x] `killswitch` CLI command, because a stop that needs a redeploy is not a stop

## §8 — tests

- [x] T27 chain reservation, 200-way — **found the `FOR UPDATE` deadlock**
- [x] T28 orphaned reservation swept, chain recovers
- [x] T29 `LIMIT_REACHED` / `POOL_EXHAUSTED` + event
- [x] T30 priority shedding, in-flight untouched
- [x] T31 oversubscription to 1.3×, spend still bounded
- [x] T32 cyclic approval policy rejected at compile
- [x] T33 escalate, then `on_expiry`
- [x] T34 `resume_token` cannot resume a different interrupt
- [x] T35 approver budget alerts, queue does not grow
- [x] T36 grant revoked mid-run, denied within the cache TTL
- [x] T37 credential rotated mid-run, no pinning
- [x] T38 secret scrubbed before state, checkpoint, log, artifact
- [x] T39 `IRREVERSIBLE` bare → `retries=0`, `requires_approval=True`
- [x] T40 `drain` — zero orphan INTENT
- [x] T41 `halt` mid-effect — INTENT enumerable
- [x] T42 corpus × every ingress
- [x] T43 every gateway denial produces an audit row with a reason

## §9 — exit criteria

- [x] **Correctness.** T27–T43 green; the corpus results documented.
- [ ] **Non-regression.** One full week with governance active, compared against the
      §0 baseline. Not run — this is a week of real operation and the milestone
      cannot fake it. `docs/M2_GATE.md` §9 is the table.
- [ ] **The denial stream reviewed once.** `python -m runtime.cli denials --days 7`
      exists and works; a week of rows to read does not yet.
- [x] The three §9 cost suspects each have a guard *and a test of the guard*: audit
      batching (asserted at one statement per call), the permission and kill-switch
      caches (TTL-bounded, shared by both gateways), one reserve/reconcile pair per
      tool call.

## Not in §2's scope, done anyway, and why

- [x] **`GovernanceSweeper`.** `Settings` has carried
      `budget_sweep_interval_seconds` since M0 and `approval_sweep_interval_seconds`
      since M1, and **nothing ran either** outside the CLI and the tests. Survivable
      in M1, where the only time-driven behaviour was a TTL nobody waited on. Not
      survivable in M2: escalation is a clock, and a chain that is never walked is a
      list. Wired into `worker/main.py` alongside the relay and the reaper.
- [x] **Audit partition maintenance** on the same sweeper. Rows in the DEFAULT
      partition are not lost but cannot be dropped by month, which is the whole reason
      the table is partitioned.
- [x] `include_object` in the Alembic env, excluding `audit_logs` from autogenerate.
      A partitioned table has twenty-odd child tables the declarative models cannot
      express, and autogenerate proposes dropping every one of them.

## Deliberately not done

- Memory, retrieval, the Context Engine, delegation, YAML control plane, a second
  department, multi-tenancy activation, HA — all out of scope per §1.
- **Model routing by `work_class`** is named in §0 as a cost lever. The mapping
  already exists (M0 machinery, M1 uses it: Opus for work, Haiku for summarisation).
  Widening it is a §9 response, not an M2 deliverable — tune it if the cost gate
  fails, with the measurement in hand.
- **Artifact summary compression**, the other §0 lever. Same reasoning.
- **`ActorKind.HYBRID`** still raises at compile. Unchanged from M0/M1.

## Caveats worth carrying into M3

1. **`analytics` resolves `publish_external` as HUMAN.** The department policy covers
   every `ic`, and analytics is one. It holds no grant for any publishing tool, so
   this is belt and braces rather than a hole — but if M3 gives analytics a tool, the
   gate is already in front of it, which is the correct surprise to have.
2. **The permission cache window is real.** Within 30s a revoked actor keeps working.
   Documented, tested in both directions, and bounded — but it is a window, and
   anything that needs revocation to be instantaneous needs `killswitch` instead.
3. **Rate-limit buckets are debited per scope, not atomically across scopes.** A call
   refused by the third policy has already spent tokens in the first two. It throttles
   *harder* under contention, which errs safe, but it is not exact.
4. **The credential key lives in the process environment.** An attacker with the
   database has ciphertext; an attacker with the database *and* the environment has
   secrets. Moving to a KMS changes `CredentialCipher` and nothing else.
