# M0 retro — what M0 taught us, folded into M1

Written before migration 008, as the M1 plan §1 requires. Four questions were asked
of the M0 code; each answer below is either a measurement or a code change, not an
opinion. The last section is the list of things M1 encodes because of them.

---

## 1. Checkpoint size

**Measured, not guessed.** One `echo_agent@1` run (two nodes, a three-field state,
one tool call) against the dev cluster:

| table | rows | avg | max |
|---|---|---|---|
| `lg.checkpoints` | 4 | 676 B | 950 B |
| `lg.checkpoint_blobs` | 4 | 122 B | 218 B |
| `lg.checkpoint_writes` | 5 | 90 B | 218 B |

So roughly **1.2 KB of checkpoint per node execution** for a state that holds
almost nothing. That is fine, and it is also the whole warning: LangGraph
checkpoints the *entire* state at every superstep, so the cost is linear in
`state_size × supersteps`, not in what changed.

M1's graphs are four to six nodes and their natural state includes a
`CompetitorReport`. At ~8 KB of JSON that is ~40 KB of checkpoint per run, per
replay, forever — for a payload that is already durably in the artifact store.

**Decision: no `DeltaChannel`. State discipline instead.**

A graph's state may hold artifact *references* (`artifact_id`, `sha256`, a
≤512-char summary) and never artifact *bodies*. This is cheaper than
`DeltaChannel`, it is one rule rather than a new channel type, and — unlike a
convention — it is testable: `tests/test_m1_state_discipline.py` invokes each M1
graph and asserts no checkpoint row exceeds `MAX_CHECKPOINT_BYTES` (8 KB).

`DeltaChannel` is deferred to M3, where the Context Engine actually needs
incremental state. If the assertion above starts failing under real workloads,
that is the signal to build it — and the test tells us the week it happens rather
than the quarter.

## 2. Lease timing

M0 defaults: `lease_seconds=30`, `heartbeat_seconds=10`, `max_lease_expiries=3`.
These did **not** need adjusting, and the reason matters more than the fact:
`Heartbeater` is its own asyncio task (`worker/lease.py`), so a tool call that
blocks for two minutes cannot starve the heartbeat. A cooperative heartbeat would
have forced the 90s lease the M1 plan anticipated; an independent one does not.

What *did* need changing is the ceiling, not the lease. `Ceilings.max_wall_clock_s`
defaults to 300 s. A `research` run that makes a search call, three fetches and a
model call at `effort=high` will exceed that, and the failure would look like a
lease problem while being nothing of the kind.

**Decision:** leases stay at 30/10. M1 actor specs raise `max_wall_clock_s` per
actor (`research` 900 s, `content` 600 s, `marketing-head` 600 s, `analytics`
120 s) and those numbers live in the actor spec, where they are frozen into the
RunSpec, rather than in `Settings`, where an operator could change them under a
run in flight.

## 3. Journal ergonomics

Two rough edges, both real, both paid down here rather than in M6.

**`checkpoint_ns` is a footgun for cyclic graphs.** `RunContext.node()` takes a
`checkpoint_ns` that the caller must remember to vary across loop iterations, or
two iterations collide on one `logical_call_id` and the second silently returns
the first's result. `echo_agent@1` is linear so M0 never hit it. M1's `research`
graph fetches N search results in a loop, so M1 would have hit it in week one.

**Fixed:** `RunContext.node()` grows an `iteration: int | None` keyword that
derives the namespace itself. `with ctx.node("fetch", iteration=i)` is now the
obvious spelling and `checkpoint_ns` is the escape hatch. Passing both raises.

**`ordinal` never needed threading through nodes** — `NodeScope` already counts it
and the `with` block already resets it. No change; recorded so the question is
closed.

## 4. Leftover `TODO`s

`grep -rn "TODO\|FIXME" src/` → **none**, anywhere, not just in `effects/` and
`run_service.py`. Nothing to pay down.

Three items in `TODO.md` under "Open before sign-off" are still open and are *not*
M1 code work:

1. `ARCHITECTURE.md` §2 invariants are reconstructed, not verbatim v3. Still open;
   still needs the v3 document, which is not in the repo.
2. `docker compose up` has never been executed on this machine. CI's job.
3. CI runner labels are placeholders.

M1 adds nothing to that list and removes nothing from it.

---

## What M1 encodes because of the above

| Finding | Where it lands in M1 |
|---|---|
| State holds refs, not bodies | `graphs/common/state.py::ArtifactRefView`; asserted by `test_m1_state_discipline.py` |
| Ceilings, not leases, are the M1 timing risk | per-actor `Ceilings` in `runtime/org/department.py` |
| `checkpoint_ns` must not be hand-managed | `RunContext.node(..., iteration=)` |
| Nothing outstanding | — |

One more thing M0 taught that the plan did not ask about, recorded because M1
inherits it directly: **`estimate_tool_cents()` returns 0 for every M0 tool.** M1
introduces the first tool with a real price (`web.search@1`) and the first with a
real consequence (`publish.external@1`), so that function stops being a seam and
starts being a number. It is now a table keyed by qualified tool name, and the
budget test asserts a priced tool actually reserves.
