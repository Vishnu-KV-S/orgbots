# M3 TODO — memory and context

Goal: give actors continuity across sessions — **and prove it pays for itself.**

**Status: the machine is built, green, and shipping dark.** All of PR-27 through PR-35
is implemented. 688 tests pass (676 fast + 12 chaos); ruff, ruff format, mypy strict and
all five import-linter contracts clean; M0's T0–T13, M1's T14–T26 and M2's T27–T43 pass
unchanged, including T7 at both durability modes. `RUNTIME_MEMORY_ENABLED` is false by
default, so a checkout with M3 merged behaves exactly like M2 —
`test_m3_department.py::test_memory_off_leaves_the_loop_exactly_as_m2_had_it` runs the
real weekly loop and asserts it. The shadow window has not been opened, no traces have
been graded, the golden set has not been built, and therefore **none of §11's quality
numbers and none of §12's exit criteria have been produced**.

M3 is the first milestone that can make the system *worse* — a wrong fact retrieved
confidently is worse than no fact — which is why the build order puts every prompt-
touching change behind one flag and every number behind a corpus that has to be
hand-labelled first. `docs/M3_SHADOW.md` holds the bars, set in advance;
`docs/M3_LIBRARY_FACTS.md` holds §3's verification.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## §1 — gate check

- [ ] **M2's numbers recorded as the M3 baseline.** Not doable here: it is a
      measurement of M2's non-regression week, which has not been run.
      `docs/M3_SHADOW.md` §0 is the table.
- [~] **Three hypotheses named.** §1 wants them from the M2 denial stream, which does
      not exist yet. `docs/M3_SHADOW.md` §1 records three *predictions from the
      department's structure*, labelled as predictions, with the query that settles
      each. Replace them with three from real data before the flip.
- [ ] Confirm the M2 non-regression week is green before opening the shadow window

## §3 — library facts, verified

- [x] All seven checked against **mem0ai 2.0.18 on 2026-08-23**; recorded in
      `docs/M3_LIBRARY_FACTS.md` with the version and date §3 asks for
- [x] Fact 4's two scope-filter bugs are **fixed** in this release — and we still do
      not rely on the library's filter (§13 risk 3); ours is the boundary
- [x] Fact 5: `MEM0_TELEMETRY` disabled *before* the import, because the module reads
      it at import time and, with it on, builds a second vector store
- [~] **Fact 3: pgvector is not installed on this machine, or in `postgres:16-alpine`.**
      See the deviation below — it is the single decision that shaped the most code.
- [~] **An eighth fact, not in §3.** Mem0 owns its own LLM, embedder, connection pool
      and SQLite history DB. Left alone that makes M3's highest-volume model calls the
      only ones in the system with no `work_class`, no budget line and no audit row —
      I12 violated, and §13 risk 4 made structurally true.

## §4 — migrations 023–029 (PR-27, PR-31, PR-33)

- [x] `023_mem_schema` — `CREATE SCHEMA mem`, pgvector probed not assumed,
      `mem.capabilities`, `mem.collections` (the registry that makes T45 testable)
- [x] `024_memory_meta` — `memory_metadata`, `memory_audit`;
      `ck_memory_quarantine_status` makes §6's rule structural
- [x] `025_entities` — `entities`, `entity_links`, relational not vector
- [x] `026_intentions` — `scheduled_intentions`, deduped while pending
- [x] `027_procedures` — `procedure_candidates`, counters not a threshold
- [x] `028_promotion` — `memory_promotions`; `ck_promotion_one_rung` and
      `ck_promotion_decided_has_reviewer` are the anti-auto-promotion constraints
- [x] `029_context` — `context_traces` **before** retrieval, plus `v_memory_grades`,
      `v_memory_token_effect`, `v_memory_inventory`
- [~] **`030_audit_embedding` is not in §4's list.** M2's `audit_logs.gateway` is a
      closed vocabulary and M3 adds a fourth gateway; `test_m3_isolation.py` failed on
      the first run. The constraint did exactly what it was built to do. Widening it
      deliberately beats the shortcut of not auditing embeddings at all.
- [x] Every one reversible; `downgrade base && upgrade head` clean (T0)
- [x] The sidecar lives in `public`, not in `mem`, so re-seeding the vector store
      cannot take the provenance with it

## §5 — shadow mode (PR-29, PR-30)

- [x] `ContextPlanner` retrieves, ranks, budgets and traces — and injects nothing
- [x] `PlannedContext.block` is `""` in shadow mode, so `assemble(memory=...)` needs no
      branch and the prompt is **byte-identical** to M2's — asserted as a string
      comparison, not argued
- [x] `would_have_injected_tokens` computed in shadow mode, so eval 8 is executable
      during phase one rather than on the day it is too late to set a budget
- [x] Shadow mode does **not** touch `access_count` — a measurement that perturbs what
      it measures is not a measurement
- [x] The flip bar written down **before** the data: `docs/M3_SHADOW.md` §2
- [x] `runtime.cli memory grade` — single-letter answers, deterministic sample,
      `--verdict` for the decision
- [ ] **Two weeks of shadow traces accumulated.** Calendar work.
- [ ] **~100 retrievals hand-graded.** §13 risk 5's failure mode.

## §6 — write path (PR-28)

- [x] `MemoryWorker` on its own consumer group over `run.succeeded` — never the hot
      path, and T44 asserts the *absence* rather than timing it
- [x] Extraction is one `work_class=MEMORY` call through `ModelGateway.complete_detached`
- [x] `run_trust` collapses to quarantine if any part of the run touched outside content
- [x] Quarantined memories are private to the originating actor and never cross-scope
- [x] At-least-once delivery made idempotent in the database; a run that produced no
      facts writes a marker so "extracted nothing" and "never looked" stay distinct
- [x] Ack-before-work, so a poison entry cannot be redelivered forever at Opus prices
- [~] **§6's rule needed a decision it does not spell out.** *"If any untrusted content
      was in the run's context"* — with M2 fencing every inbox message unconditionally,
      that quarantines every run in the department and the rule returns one answer
      forever. So trust is computed from *provenance* (the effect journal, plus
      inherited taint across a correlation chain) rather than from *fencing*. Both
      error directions are stated in `runtime/memory/service.py` and the one real gap —
      content arriving through a run's **input** — is recorded in `docs/M3_SHADOW.md` §6.
- [~] **Most weeks, everything will be quarantined.** `research` fetches every week and
      taint is inherited across the week's chain. That is §6 working as written, and it
      makes **promotion review the load-bearing path** for M3 producing any
      department-scoped memory at all.

## §7 — read path (PR-29, PR-32)

- [x] Scope filter built from the run's own context; nothing a node passes can widen it
- [x] `status='active' AND trust != quarantine` **in the query, not after**
- [x] Rerank in `domain/memory.py`, weights in `Settings` so tuning shows in a diff
- [x] Hard token cap; `top_k` and `K` in config, both `[CHOSEN]`
- [x] Retrieval runs **in parallel** with the node's other context assembly
- [x] Embedding cache (in-process LRU, keyed on `embedding_version`)
- [x] Adaptive budget: `InjectionBudget` caps count *and* tokens *and* per-memory chars
- [~] **Artifact-summary compression is not what §2 implies.** `artifacts` has no
      summary column, so the extractor gets *descriptors* — kind, size, digest — rather
      than summaries. That turns out to be the right shape: an extractor handed eight
      fetched pages re-does the research actor's job at Opus prices.
- [~] **Rerank is additive, not multiplicative**, despite §7's `recency x importance x
      similarity x access decay`. A product makes any zero term annihilate the row, and
      `access_count = 0` is the state every new memory starts in.

## §8 — promotion (PR-33)

- [x] One rung at a time, enforced by a CHECK constraint
- [x] Cheap-model classifier with the four criteria as **booleans**, plus a human sample
- [x] `approve` recomputed from the criteria, not taken from the model
- [x] Quarantine checked before the model call *and* again at apply
- [x] `reverse()` — narrowing needs no review, because requiring approval to undo a
      mistake is how mistakes stay
- [x] T53 asserts the absence of a second path by reading the source
- [ ] **The queue actually worked.** Calendar work, and see the §6 caveat: if nobody
      reviews, M3's effect on the department is close to zero.

## §9 — build order

- [x] PR-27 … PR-34 touch no prompt; `RUNTIME_MEMORY_ENABLED=false` is M2 exactly
- [x] PR-35 is one flag, per actor, off by default
- [x] `test_only_the_injection_flag_changes_a_prompt` is §9's sentence as an assertion
- [x] `test_m3_department.py` runs the **real weekly loop** with a subsystem attached to
      the real `Worker` and asserts on the prompt the *provider* received — because
      everything between the planner and the model is wiring, and wiring breaks quietly

## §10 — the golden set (PR-34)

- [x] `GoldenBuilder` writes `facts.jsonl`, `queries.jsonl`, `contradictions.jsonl`
      from real runs — unlabelled, with a `TODO` marker on every query
- [x] `leakage.jsonl` and `quarantine.jsonl` ship, synthetic **by design**: they are
      isolation probes, not quality samples
- [x] `quarantine.jsonl` is keyed on M2's injection payload names, so eval 9 and
      `docs/INJECTION_RESULTS.md` name the same attacks
- [x] `PROVENANCE` is `placeholder`, carried onto every `EvalResult`, and a test fails
      if real facts are committed under it
- [x] Eval 2 **refuses to score** unlabelled queries rather than returning 0% or 100%
- [ ] **The day of hand-labelling.** §13 risk 5.

## §11 — tests and evals (PR-34)

- [x] T44 — `add()` never on the request path
- [x] T45 — embedder swap → new collection + backfill; old untouched; dimension refused
- [x] T46 — non-list / non-UUID scope filter rejected before the store
- [x] T47 — special characters cannot forge a scope; the SQL is bound, not built
- [x] T48 — cross-scope leakage: zero, over 20 probes per scope pair
- [x] T49 — cross-actor leakage: zero, end to end through the planner
- [x] T50 — quarantine invisible to another actor; the DB refuses active+quarantined
- [x] T51 — contradiction supersedes, and the old fact stops being retrieved
- [x] T52 — a run that fetched a page produces quarantined memories, all of them
- [x] T53 — promotion requires a review row; no auto-promotion path exists
- [x] T54 — a `context_traces` row for every retrieval, shadow or live
- [x] The nine evals implemented, with **provenance and `measured` on every result**
- [x] Evals 4, 5, 9 return zero on a correct store **and non-zero on a broken one** —
      the control is the most important test in `test_m3_evals.py`
- [ ] Evals 1, 2, 3, 6, 7, 8 producing numbers. Needs the corpus and the grading.

## §12 — exit criteria

- [x] T44–T54 green
- [x] Evals 4, 5, 9 at zero
- [ ] Evals 1, 2, 3, 6 at the bars in `docs/M3_SHADOW.md` §3
- [ ] One week with injection on, measured against M2 within §12's tolerances
- [ ] H1, H2 and H3 answered explicitly from `context_traces`

**If rejection rate does not improve and cost is up, memory has not earned its place.**
Keep it in shadow mode, keep traces accumulating, move to M4. §12 says that is a
legitimate outcome and the code is not wasted.

---

## Deliberate deviations

| Deviation | Reason |
|---|---|
| `MemoryStore` is a **port with two adapters**, and the default is not Mem0 | pgvector is absent from stock Postgres and from this repo's dev stack (fact 3). M3's three zero-tolerance gates would otherwise be conditional on an optional system package, which is how a hard gate becomes decorative. `NativeMemoryStore` does exact cosine over `real[]` in SQL — correct at M3 volumes, and with no recall parameter to confound eval 1. `RUNTIME_MEMORY_STORE=mem0` is the production path and never falls back silently. |
| Mem0's LLM and embedder are **re-registered** to route through the gateways | Fact 7. Otherwise extraction, fusion and embedding — the highest-volume model calls in M3 — are the only ones with no `work_class`, no budget reservation, no kill switch and no audit row. The injection points are plain class attributes; the loop bridge is safe because `AsyncMemory` wraps sync calls in `asyncio.to_thread`, and `_bridge` asserts that rather than assuming it. |
| The isolation boundary is **ours**, not the library's | §13 risk 3 names it as a risk; making it ours removes it. `MemoryMetadataRepository.authorize` is the predicate; the store's filter is a prefilter. A library regression costs recall, not isolation — and T48/T49 hold against either adapter because they test the same SQL. |
| Embeddings get their **own gateway**, not a helper | An embedding call spends money and talks to a provider with a quota, which is exactly the pair of facts that made M2 govern `ModelGateway`. Same pipeline, same audit row, same `work_class`. |
| `ModelGateway.complete_detached` exists | The memory worker runs after a run has finished: no lease, no fence, no spec, no `RunContext`. The two alternatives — fabricating a spec, or letting the worker hold its own client — are both worse. Ceilings do not apply and the budget pool is the bound; that trade is stated at the method. |
| Migration `030` widens `audit_logs.gateway` | Found by a failing test, not by review. See §4 above. |
| Topic constants moved to `runtime.events.topics` | `MemoryWorker` subscribes to `run.succeeded` from *below* `runtime.runtime`, and a subscriber importing a topic name upward is a layering violation. `lint-imports` caught it on the first run. |
| `work_class=MEMORY` is outside `M1_WORK_CLASSES` | So memory spend lands in `overhead_ratio` and makes the coordination ratio *worse* rather than better — the same asymmetry that protects every other number in this codebase. |
| Extraction runs on **Opus** | §13 risk 6, taken literally: *"this is the wrong place to economize."* Every other cheap-model choice here is reversible by re-running the call; this one is not, because the bad fact is already in the store being retrieved. §13 risk 4 is the counterweight and it is why the class is on the dashboard from day one. |
| Consolidation does **not** summarise | Eval 7 needs a number before compression runs, or "did compression lose anything" is unanswerable while it is happening. Pruning and proposal hygiene only. |
| Trust is computed from provenance, not from fencing | See the §6 caveat. The literal reading of §6 quarantines every run in this department forever. |
| `MEMORY_PROFILE` is not in any `ActorSpec` | Memory calls are made by the worker after a run, not by an actor during one, so adding it would change every `spec_hash` in the department to describe a call the run cannot make. |
