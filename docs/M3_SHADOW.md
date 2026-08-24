# M3 — the bars, set before the data

M3 §5: *"**Set the bar for flipping injection on before you start, and write it down.**
... What matters is that the bar exists before you see the data, not what the bar is."*

M3 §11: *"Bars marked `[CHOSEN]` are starting positions, not findings. Set them before
you measure."*

This is that document. **Nothing in it is a measurement.** It is a set of positions
taken in advance, and the only property that makes it worth anything is that it was
written before there was any data to adjust it against. Every number is marked with
where it came from, using §0's tags.

| | |
|---|---|
| Written | **2026-08-23**, before the shadow window opened |
| Shadow window opened | ______ |
| Sample reached 100 graded | ______ |
| Flip decision made | ______ |
| Injection week ran | ______ to ______ |

---

## 0. Carry the baselines forward

§1: *"Carry forward both baselines. M2's numbers are the reference, not M1's."*

```
                              M1 baseline    M2 (post-governance)   M3 (injection week)
Cost per accepted outcome     ______         ______                 ______
Rejection rate                ______         ______                 ______
Unassisted completion rate    ______         ______                 ______
Coordination ratio            ______         ______                 ______
P95 run latency               ______         ______                 ______
Approval-blocked runs/week    ______         ______                 ______
Injected tokens / call        n/a            0                      ______
work_class=MEMORY spend/week  n/a            n/a                    ______
```

`docs/M2_GATE.md` is where the middle column comes from. **Neither measurement week has
been run**, which means M3's exit criteria cannot be evaluated yet either — §12's whole
comparison is against a column that is currently blank.

## 1. The three hypotheses

§1: *"From the M2 denial-stream review, name anything memory would plausibly fix... **If
you cannot name three from real run data, that is itself a finding**: the work may not
be memory-shaped, and deferring M3 in favour of M4 is a legitimate decision."*

The denial stream has not been reviewed, because M2's week has not been run. So these
three are **predictions from the department's structure**, not findings from its data,
and they are labelled as such. Replace them with three from real runs before the flip.

| # | Predicted re-derivation | How to check it afterwards |
|---|---|---|
| H1 | The Monday plan re-derives the same competitor set every week, because `marketing_head` sees last week's *metrics* but not last week's *conclusions*. | `context_traces` where `node='plan'`, grouped by week: did the same memory ids recur, and did the plan stop repeating the same assignments? |
| H2 | `research` re-fetches sources it already summarised, because `web.fetch@1` has no memory of what it read. | Join `effect_intents` on `tool_name='web.fetch@1'` across weeks and count repeated URLs before and after the flip. |
| H3 | `content` re-asks the head about house style and audience, because that is department knowledge with nowhere to live. | The coordination ratio, and `inbox_messages` from `content` to `marketing-head` per week. |

§12 requires answering these **explicitly** after the injection week — *"Query
`context_traces` for those specific cases"* — not summarising them.

## 2. §5's flip bar `[CHOSEN]`

The bar for turning injection on, in `runtime.memory.grading.GradingBar`:

```
helpful-or-neutral   >= 60%        of a hand-graded sample
harmful              <=  5%        of the same sample
sample size          >= 100 traces
```

These are §5's suggested starting position, taken unchanged. **No reason to deviate was
available**, which is the honest state: a bar chosen from experience would be better and
there is no experience yet.

Check it with:

```bash
uv run python -m runtime.cli --organization $ORG memory grade --verdict
```

Exit code 2 means the bar is not met. The command deliberately **will not flip the
flag** — that is `RUNTIME_MEMORY_INJECTION_ENABLED` and a worker restart, and it should
be done by someone who read the verdict.

`[ESTIMATE]` §5 puts two weeks of shadow mode on a four-actor loop at enough retrievals
to grade. Check `memory status` for the actual trace count rather than assuming.

## 3. §11's eval bars `[CHOSEN]`

In `runtime.memory.evals.EvalBars`. Changing one is a diff.

| Eval | Bar | Kind |
|---|---|---|
| 1 Fact recall | ≥ 0.80 | `[CHOSEN]` |
| 2 Retrieval precision | ≥ 0.60 | `[CHOSEN]` |
| 3 Irrelevant rate | ≤ 0.05 | `[CHOSEN]` |
| 4 Cross-actor leakage | **0** | hard gate |
| 5 Cross-scope leakage | **0** | hard gate |
| 6 Contradiction handling | 1.00 | §11 states it |
| 7 Summary fidelity | ≥ 0.90 | `[CHOSEN]` |
| 8 Token effect | **no bar** | §11: measure, then budget |
| 9 Quarantine containment | **0** | hard gate |

`runtime.cli memory evals` exits 2 if a **hard gate** fails and 0 otherwise, including
when a `[CHOSEN]` bar is missed. That asymmetry is deliberate: evals 1, 2, 3, 6 and 7
are numbers over a corpus that may still be a placeholder, and failing CI on a guess
teaches people to ignore CI.

**Eval 8 gets no bar here and will not get one from this document.** §11: *"You cannot
know what injected memory costs per call until you measure it in your own system."*
Measure it in shadow mode, write the number in §0's table, then set
`RUNTIME_MEMORY_MAX_INJECTED_TOKENS` — the current value of 900 is a stop, not a budget.

## 4. §12's tolerances `[CHOSEN]`

For the one injection week, against the M2 column:

| Metric | Requirement |
|---|---|
| Rejection rate | improved, or unchanged with a stated reason |
| Cost per accepted outcome | within **+15%** |
| Unassisted completion rate | no worse |
| Coordination ratio | no worse |
| P95 latency | within **+20%** |
| Injected tokens/call | inside the budget set from eval 8 |

§12's own words about the two percentages: *"The two `[CHOSEN]` tolerances are
arbitrary. Pick them before the week starts. Their only job is to stop you from
accepting an arbitrarily bad trade after the fact."* Taken unchanged.

**If rejection rate does not improve and cost is up, memory has not earned its place.**
Keep it in shadow mode, keep traces accumulating, move to M4. That is a legitimate M3
outcome and the code is not wasted.

## 5. What the numbers will be measured *under*, and why it matters

Two configuration facts change what evals 1 and 2 mean, and both are true of this
repository as shipped:

**The embedder is `HashingEmbedder`.** Feature hashing over word unigrams and bigrams —
real lexical similarity, no paraphrase at all. "What do they charge" and "pricing" land
orthogonally. Eval 1 and eval 2 are measuring the embedder as much as the retrieval, and
turning on a real one (`RUNTIME_EMBEDDING_ENDPOINT_URL`) should move them. Record which
embedder produced any number you quote.

**The store is `NativeMemoryStore`.** Exact cosine over `real[]` in Postgres, because
pgvector is not installed here (`docs/M3_LIBRARY_FACTS.md` fact 3). Exact search has no
recall parameter, so eval 1 is not confounded by an index — which is a *better* position
for measuring, and a worse one for scaling past the hundred-thousands.

## 6. Known gaps, written down rather than discovered

**The `run_trust` input-path hole.** §6's rule is implemented over two signals: the run's
own effect journal, and inherited taint across a `correlation_id` chain. It does **not**
catch content that entered through a run's *input* — a human or an API caller pasting a
payload into `POST /v1/runs`. T52 covers the fetch path and does not cover that one. The
fix is a trust level on the run input at admission; it is not in M3's scope and it is
recorded here so it is a position rather than an oversight.

**Everything in the marketing department will be quarantined most weeks.** `research`
fetches pages every week, and taint is inherited across the week's correlation chain, so
in a typical week every actor's extracted facts land quarantined and private. That is
§6 working as written, not a bug — and it means **promotion review is the load-bearing
path for M3 producing any department-scoped memory at all**. If the promotion queue is
not being worked, M3's effect on the department will be close to zero and the injection
week will measure nothing. Watch `memory promotions` alongside `memory grade`.

**Consolidation does not summarise.** `MemoryService.consolidate` prunes and withdraws
stale proposals; it does not compress clusters of memories into `DERIVED` facts. That is
why eval 7 reports "not measured" rather than 100%. Summarising consolidation should not
be turned on until eval 7 has a number, because "did compression lose anything" needs to
be answerable before compression is running.

## 7. The order this happens in

1. **PR-27 … PR-34 merged.** None of them touches a prompt (§9), so this is safe during
   an M1 or M2 measurement window. `RUNTIME_MEMORY_ENABLED` stays false until the
   baseline weeks are finished.
2. **Turn the subsystem on, injection off.** `RUNTIME_MEMORY_ENABLED=true`. This is
   shadow mode: retrieval runs, traces accumulate, prompts are byte-identical
   (`test_m3_shadow.py` holds that).
3. **Two weeks.** `memory status` to watch the trace count.
4. **Grade ~100.** `memory grade`. Then `memory grade --verdict`.
5. **Build and label the golden set.** `memory golden --build`, then the day of
   labelling, then `PROVENANCE` → `real`. Then `memory evals` means something.
6. **Flip, one actor first.** `RUNTIME_MEMORY_INJECTION_ENABLED=true` plus
   `RUNTIME_MEMORY_INJECTION_ACTORS=research`. This is PR-35 and the only change that
   alters what a model sees.
7. **One week, all actors, measured against §12.**
8. **Answer H1, H2 and H3 explicitly.**
