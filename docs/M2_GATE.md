# M2 gate — the numbers, and how to get them

M2 is the first milestone with a **non-regression requirement**, and §9 is blunt
about why: everything M2 adds costs something. Governance that makes the
organization 40% more expensive has not made it safer, it has made it unaffordable,
and the second fact will win the argument eventually.

This document is the worksheet. §0 is the baseline you record *before* starting; §9
is the comparison you run after. Neither can be produced by the test suite, and that
is deliberate — they are measurements of a week of real operation, and a measurement
the implementation can produce for itself is not a measurement.

---

## §0 — the baseline, recorded before M2 was switched on

Fill this from `python -m runtime.cli --organization <id> dashboard --json` over M1's
last clean weeks. Record the *dates*, not just the numbers: a baseline whose window
nobody wrote down cannot be re-derived later.

```
Cost per accepted outcome     ₹____ / $____
Rejection rate                ____%
Unassisted completion rate    ____%
Coordination ratio            ____%     (overhead ratio ____%)
AUTO_ACCEPTED share           ____%
False-accept rate             ____%
Mean edit distance            ____
Evaluated task count          ____
Clean-run dates               ____ to ____
P95 run latency               ____ms
```

**If any M1 §9 stop condition is still true, stop.** M2 is premature and M1 §10 is
the document to be reading. Governance on a system that does not yet produce work
worth governing is the most expensive way to discover that.

**Note which metric was marginal.** M2 has two levers that help cost — model routing
by `work_class`, artifact summary compression — and four that hurt: permission
checks, audit writes, reservation round-trips, and the approval queue's wall-clock.
If cost per accepted outcome was already the tight one, plan for net-neutral rather
than hoping.

---

## §9 — the comparison, after one full week with governance active

Re-run the M1 weekly loop for one week with everything in M2 §1 switched on. Same
department, same four actors, same rubrics. Then:

| Metric | Requirement | Baseline | Measured | Verdict |
|---|---|---|---|---|
| Cost per accepted outcome | within **+10%** | | | |
| Rejection rate | no worse | | | |
| Unassisted completion rate | no worse | | | |
| Coordination ratio | no worse | | | |
| P95 run latency | within **+25%** | | | |
| Approval-blocked runs | **recorded** — new operating cost | n/a | | |

### If cost is up more than 10%

§9 names the three suspects, in the order they are worth checking. All three are
fixable in a day, and all three have something in the codebase that is supposed to
prevent them — so the first question is always "did the guard stop working" rather
than "where shall I optimise".

1. **Audit writes not batched.** `AuditBuffer` collects a call's decision rows and
   writes them in one statement.
   `test_m2_audit.py::test_decision_rows_are_written_in_one_statement_per_call`
   asserts it. If that test still passes and cost is up, this is not the cause.

2. **Permission checks not cached.** `PermissionCache` and `KillSwitchService` hold
   30s and 10s TTLs. Check `pg_stat_statements` for the grant and kill-switch reads:
   a query count that scales with *tool calls* rather than with *actor-minutes* means
   a cache is being constructed per call instead of shared. The worker builds one of
   each and hands them to both gateways; a code path that builds its own would look
   exactly like this.

3. **Reservation round-trips added per call instead of per node.** One
   `reserve`/`reconcile` pair per tool call is the design and it is two round trips.
   Three or more means something is reserving inside a loop.

A fourth, specific to M2 and not in §9's list: **the audit table's write volume
itself**. Every allowed call now writes a row, which is the point — a denial rate
needs a denominator — but it is genuinely new I/O. If it dominates, the lever is
severity-based sampling of `allowed` rows, *not* dropping them: a denial rate
computed against a sampled denominator is still a rate, and one computed against no
denominator is a count.

### Do not carry a regression into M3

§9's last sentence is the one that matters most. M3 adds memory retrieval, which has
its own overhead. Two overlapping regressions are not twice as hard to diagnose, they
are much harder, because the obvious experiment — turn one off — stops being
available.

---

## Also required: the denial-stream review

§9 makes this an exit criterion rather than a nicety.

```
python -m runtime.cli --organization <id> denials --days 7
```

Read every line. Each one is a finding of the form *"this actor repeatedly attempted
something it lacks"*, and there are only two explanations:

- **its authority is wrong** — the actor genuinely needs the thing, and the policy or
  grant should be widened deliberately, with a diff; or
- **its prompt is wrong** — the actor believes it has something it does not, which
  means the actor spec block or the task input is misleading it.

Both are worth having before M3, and neither shows up anywhere else. A week with zero
denials is not a pass — it usually means the actors never tried anything interesting,
or that grants are wider than the work requires.

Record the review here:

```
Week reviewed         ____ to ____
Total decisions       ____
Denials               ____  (____%)
Distinct findings     ____
Authority changed     ____   (list them)
Prompts changed       ____   (list them)
```

---

## The injection corpus

`docs/INJECTION_RESULTS.md` is generated by
`tests/test_m2_injection.py::test_t42_writes_the_results_document` and regenerated on
every run. §9 requires the results to be documented *including known-failing payloads
with a written rationale for each* — the "Known gaps" section of that file is where
the rationale lives, and it is prose that has to be re-read rather than a table that
can be glanced at.

Re-run the corpus whenever a graph or a prompt changes. §8: *"T42 is the one that will
find real problems."*

---

## What the test suite does and does not establish

Green tests establish **correctness**: T27–T43 are the mechanisms working. They
establish nothing about **cost**, which is why §9 exists and why this document cannot
be filled in by CI.

The distinction is the same one M0 and M1 drew and it is worth restating: M0 was
pass/fail on tests, M1 was a measurement, and a measurement can be gamed by the person
who wants the answer to be yes. `docs/MEASUREMENT_PROTOCOL.md` is what stops that for
M1's numbers, and it applies unchanged here — the baseline is frozen before the change,
not chosen after it.
