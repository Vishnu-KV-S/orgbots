# M4 TODO — the config plane

Goal: define the organization in YAML instead of Python, **without changing what it
does**.

**Status: built, green, and provably behaviour-neutral.** PR-36 through PR-44 are
implemented. 845 tests pass (156 of them M4's); ruff, ruff format, mypy strict and all
five import-linter contracts are clean; M0's T0–T13, M1's T14–T26, M2's T27–T43 and
M3's T44–T54 pass unchanged.

The §8 round-trip is green: exporting the M1 department to YAML, compiling it back and
hashing every actor produces **byte-identical `spec_hash` values** to the Python
definitions. That is the entire claim M4 needs to make, it is asserted on every CI run
by `tests/test_m4_roundtrip.py`, and it is why this is the one milestone that needs no
non-regression week.

```
marketing-head   cc9f53c23d58f76177a6f42247897ddf
research         e0bc8784d10e431ecd3c8b775db6886c
content          2d83f90a6684a8a49b4a47f1156d736c
analytics        702aeae039b6c0eb69990a425539172b
```

`config/org/` holds the department as 25 documents. `runtime.org.department` and
`runtime.org.governance_seed` are unchanged and still ship the same values — the
exporter reads them, so the two cannot drift without a test failing.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## §3 — the document format

- [x] Envelope: `apiVersion: agent-platform/v1`, `kind`, `metadata`, `spec`;
      `extra="forbid"` at every level
- [x] Eleven kinds: Organization, Department, Role, Actor, ModelProfile,
      MemoryProfile, ToolGrant, Connection, BudgetPolicy, AuthorityPolicy, Trigger
- [x] camelCase on the wire, snake_case in Python, one alias generator
- [x] One kind per document, several documents per file, `---` separated
- [x] **Secrets are never inline** — `{secretRef: name}` only, a validation *error*
- [x] **References stay pinned** — `web.search@1`, `research@1`; unpinned is an error
- [~] `budget:` is a document, not an inline actor field. See the deviations.

## §4 — identity and the rename problem

- [x] `metadata.name` is the identity key within an organization
- [x] **Option (b) taken**: a plan that removes an actor with run history *and* creates
      one is refused; `--rename OLD=NEW` confirms a rename, `--allow-replace` confirms
      it is not one
- [x] A rename keeps the actor row, its id, its versions and its task history — and
      repoints every `reports_to` that named it
- [x] Option (a)'s `actors.uid` exists (migration 033), nullable, set by nobody

## §5 — the compilation pipeline

- [x] `yaml.safe_load` only (edge case 86); an unsafe tag fails to parse
- [x] Pydantic per kind → semantic validation → dependency graph → canonical compile
- [x] **The compilation target is `ActorSpec` itself**, not a parallel type, so §8 is a
      value comparison rather than an equivalence argument

## §6 — semantic validation

Every row has a positive and a negative test in `tests/test_m4_validation.py` (53).

- [x] Referenced department exists; the department tree is acyclic
- [x] `reportsTo` resolves, is not self, and the reporting graph is acyclic
- [x] **Authority escalation acyclicity**, re-run against the new config — M2's
      `check_escalation_acyclicity`, imported rather than reimplemented
- [x] Approver strictly up-hierarchy, including for actor-scoped policies, which M2's
      row-level check structurally cannot see
- [x] Tool references exist **at the pinned version**
- [x] Graph and handler references exist in the registry
- [x] Memory scopes exist and the actor may hold them (edge case 81)
- [x] Delegation child authority ⊆ parent authority, statically over the reporting edge
- [x] Deterministic worker has no `modelProfiles` and `maxLlmCalls == 0`
- [x] `HYBRID` declared → refused
- [x] Budget sums: children ≤ parent × (1 + oversubscription)
- [x] No inline secrets; no unpinned references
- [x] Extra: an actor's `tools` must be covered by a `ToolGrant`, and a
      provider-side `webSearch` needs a `web.search` grant

## §7 — two-phase apply

- [x] Phase 1 writes entities with reference columns NULL; phase 2 fills them in
- [x] **Every** reference column is deferred, not just the department/head cycle — so a
      reporting loop gets `validation`'s message instead of a generic sort failure
- [x] Both phases in **one** transaction; `test_a_failure_in_phase_two_rolls_back_phase_one`
- [x] `pg_advisory_xact_lock` on `organization_id`; transaction-scoped, so a crashed
      applier cannot leave an organization locked
- [x] The M2 compile-time checks run **inside** the transaction, as `seed_governance`
      does

## §8 — the exit test that matters

- [x] Round-trip equivalence green for **every** actor, in memory and against the
      committed corpus
- [x] Equality as well as hash equality, in case `canonical_hash` has a blind spot
- [x] Export is byte-stable, so `git diff` after an export means something
- [x] In CI, permanently

## §9 — migrations

- [x] `031_spec_docs` — `spec_documents` (source YAML beside the compiled JSONB)
- [x] `032_apply_history` — `apply_plans`, `apply_events`
- [x] `033_identity` — `actors.uid`, `actors.reports_to`, `actors.active`
- [x] `plan_hash` recheck, and `[CHOSEN]` 15-minute staleness
- [x] T0 (up, down, up) still green

## §10 — build order

| PR | Content | |
|---|---|---|
| 36 | Migration 031; `safe_load` loader; per-kind models; `validate` | [x] |
| 37 | Exporter, **shipped before the importer was finished** | [x] |
| 38 | Semantic validation, each check with its own test | [x] |
| 39 | Dependency graph, topological order, two-phase resolution | [x] |
| 40 | Canonical compile + hash; **the §8 round-trip test** | [x] |
| 41 | Migration 032; differ, `plan`, `diff`; `plan_hash` and staleness | [x] |
| 42 | Transactional `apply` + advisory lock + rollback tests | [x] |
| 43 | `spec drift` and `spec show` | [x] |
| 44 | Migration 033; rename guard; CLI polish; docs | [x] |

## §11 — edge cases

| # | Scenario | Test |
|---|---|---|
| 72 | Apply while runs are in flight | `test_an_in_flight_run_keeps_its_pinned_spec` |
| 73 | Deactivating an actor with live runs | `test_deactivating_an_actor_with_live_runs_is_refused` |
| 74 | Failure in phase 2 | `test_a_failure_in_phase_two_rolls_back_phase_one` + `test_m4_chaos` |
| 75 | Two concurrent applies | `test_a_concurrent_apply_is_refused_not_interleaved` |
| 76 | Plan/apply skew | `test_applying_a_plan_whose_state_moved_is_refused`, `..._older_than_the_window` |
| 77 | Rename with run history | `test_a_removal_plus_a_creation_is_refused_without_intent` |
| 78 | Actor removed from YAML | `test_removing_an_actor_deactivates_it_and_keeps_its_versions` |
| 79 | Budget lowered below committed | `test_lowering_a_budget_below_committed_is_refused_with_the_figure` |
| 80 | Escalation cycle from a config edit | `test_an_escalation_cycle_is_refused` |
| 81 | Scope added an actor may not hold | `test_an_actor_may_not_name_another_departments_scope` |
| 82 | Inline secret | `test_a_literal_under_a_credential_key_is_an_error_not_a_warning` (+7) |
| 83 | Unpinned reference | `test_an_unpinned_tool_is_refused`, `test_an_unpinned_graph_is_refused` |
| 84 | Canonical form drifts from Python's | `tests/test_m4_roundtrip.py`, in CI |
| 85 | Unstable diff ordering | `test_plan_ordering_is_canonical`, `test_plan_hash_ignores_the_timestamp` |
| 86 | Unsafe YAML tags | `test_a_yaml_tag_cannot_construct_a_python_object` |
| 87 | Database changed outside YAML | `test_drift_reports_a_hand_granted_tool` (+4) |
| 88 | Apply during a measurement window | Operational rule — §12, not code |

## §12 — exit criteria

**Hard:**

- [x] §8 round-trip equivalence green for every actor
- [x] Edge cases 72–87 have tests
- [x] Every §6 validation has a positive and a negative test
- [x] `apply` is provably transactional: `tests/test_m4_chaos.py` SIGKILLs the process
      in three different phases and asserts every table is empty afterwards
- [x] The M1 department is defined in YAML, applied, and produces identical
      `spec_hash` values

**Soft:**

- [x] `spec drift` returns clean against a freshly applied org

**No non-regression week required**, and that is the point.

**Operational rule going forward:** do not run `apply` during a measurement window.
M4 makes the org easy to change, and a two-week clean run means two weeks without
changes. The tool does not enforce this — you do.

---

## Deliberate deviations

| Deviation | Reason |
|---|---|
| **No `departments` table.** A `Department` document lives in `spec_documents`; the department itself stays the text column on `actors` and `roles` that M2 made it. | §9 lists exactly three migrations and none of them is a department table. Adding one would mean re-pointing `budget.department_scope_id`, `memory.department_scope_id` and every M2 policy scope at a foreign key — a schema change with a runtime blast radius, inside the one milestone whose claim is that runtime behaviour is untouched. The document still carries the head, the parent and the description, and `--rename` for a department is M5's problem if it ever becomes one. |
| **Every reference column is deferred to phase 2**, not only the department/head cycle §7 names. | Same mechanism, better errors. Deferring only the one documented cycle meant a self-parenting role or a reporting loop came out of the topological sort as "dependency cycle among 17 documents", which names every document that transitively depended on the two at fault. Deferring all of them puts those failures in `validation`, where the message names the two documents involved. |
| **`apply` publishes a new actor version only when the hash moved.** `seed_department(republish=True)` still publishes unconditionally. | M0's argument for unconditional republish is that versions are cheap and ambiguity is not. It does not survive a control plane: an apply that churned a version for every actor would make "nothing changed" and "everything changed" indistinguishable in `actor_versions`, and §13 risk 2 is precisely that the canonical form drifts and nobody notices. The two paths differ deliberately and both are tested. |
| **`BudgetPolicy` is one document, not a `budget:` block on each actor** — despite §3's illustrative example. | The check that matters is *children ≤ parent × (1 + K)*, and a limit scattered across a dozen actor documents makes that sum something a reviewer assembles by hand from a diff. One document is one place to look. Per-actor limits are still expressible, in `spec.actors`. |
| **The exported `BudgetPolicy` sets the org and the department only.** | `BudgetService` gives every actor `DEFAULT_ACTOR_LIMIT_CENTS` independently, which sums to 1.67× the department pool. That is legal — reservations are bounded per level and allocations are advisory — but it is not an *allocation*, and writing it down as one would fail §6's own budget check. Exporting the two levels that are real means applying the document changes no pool limit, which is the property M4 needs. |
| **`apply_plans.organization_id` has no foreign key**, alone among `organization_id` columns. | A plan can be saved for an organization that does not exist yet — that is the *first* apply, the one most worth reviewing before running. An FK would make `plan --save` fail exactly then. `apply_events` keeps its FK, because an event is only written after the apply has created the organization. |
| **`MemoryProfile` documents are validated and stored but do not reach `ActorSpec`.** | `ActorSpec` has no memory field; `RunSpec.memory_scopes` is resolved at admission. Adding one would change every `spec_hash` in the department and fail §8 — to describe a narrowing the runtime already computes. The scopes are checked (edge case 81) and recorded; wiring them into admission is a runtime change and therefore not M4's. |
| **No `--drain` flag.** Edge case 73 refuses instead, and names the kill switch. | §11 offers "refuse, or `--drain` which blocks new runs and waits". A CLI that blocks for an unknown number of minutes while holding an advisory lock is worse than one that tells you what to do; `killswitch --engage --mode drain` already exists and does exactly the first half. |
| **`BudgetService.apply_limits` and `BudgetRepository.set_pool_limit` are new.** | Edge case 79 needs to compare a proposed limit against `committed + reserved` and refuse. It goes through the budget service rather than through raw SQL because `budget-tables-are-private` says so, and it locks `FOR NO KEY UPDATE` rather than `FOR UPDATE` because `test_m2_governance` caught the latter — it deadlocks against the `FOR KEY SHARE` an FK insert takes on the parent pool. That test was written for T27 and it found this. |
| **`runtime.spec` is a new layer, above `runtime.runtime`.** | It needs the cron parser and the graph and handler registries — the same imports `department_boot` makes for the same reason. It sits *below* `runtime.worker` and `runtime.api` so that nothing which executes a run can reach the config plane: a run executes under its frozen RunSpec, and a worker that could import the compiler could be made to consult live configuration. |
| **`spec validate` runs in CI's *static* job**, not the test job. | It touches no database, which is the whole argument for the verb existing. A pull request that breaks `config/org` fails without a Postgres and fails on the malformed document rather than on a deploy. |

---

## What M4 deliberately did not build

- **A reconciler.** `apply` is a command, not a daemon. "Apply on git push" is one
  small step and turns a command into something that can rewrite the org unattended,
  during a measurement window (§2, §13 risk 4).
- **`spec drift --fix`.** Drift reports and never heals. The obvious next feature after
  `--fix` is the daemon above.
- **A UI, GitOps automation, remote state, multi-tenancy activation, a secrets backend.**
  All out of scope in §2. `secretRef` resolves to M2's encrypted store.
