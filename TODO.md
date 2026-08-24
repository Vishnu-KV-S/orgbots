# M0 TODO

Runtime correctness milestone. Exit criterion: **T7** — kill a worker mid-tool-call,
the run resumes, the effect journal proves the tool executed exactly once.

**Status: T0–T13 green.** 142 tests pass; ruff, ruff format, mypy strict and all
four import-linter contracts clean. T7 verified at 50 iterations × 2 durability
modes on 32 cores and again pinned to a single CPU — 200 SIGKILL-and-recover cycles,
one effect row each time.

Legend: `[x]` done · `[~]` done with a caveat noted below · `[ ]` outstanding

---

## PR-1 — Repo skeleton

- [x] `pyproject.toml`: package layout `src/runtime`, py3.12
- [x] ruff config (lint + format), mypy strict, pytest/asyncio config
- [x] `.importlinter` — layers contract + gateway-only contract
- [x] `docker-compose.yml` — Postgres 16, Redis 7, MinIO
- [x] Local-cluster fallback (`scripts/devstack.sh`) for machines without Docker
- [x] `GET /healthz`
- [~] `ARCHITECTURE.md` — the fourteen invariants are **reconstructed**, not
      verbatim. See "Open before sign-off" below.
- [x] `.github/pull_request_template.md` asking which invariants the change touches
- [x] CI workflow: ruff · mypy · import-linter · pytest on 16-core + 1-vCPU

## PR-2 — `domain/` (pure, zero I/O)

- [x] `ids.py` — typed UUID newtypes
- [x] `enums.py` — ActorKind, RunStatus, WorkClass, EffectStatus, BlastRadius,
      RecoveryPolicy, TrustLevel, AuditSeverity, ReservationStatus
- [x] `specs.py` — ActorSpec, RunSpec, ModelProfiles, Ceilings, StartRunRequest
- [x] `errors.py` — the full error taxonomy
- [x] `hashing.py` — canonical JSON + `spec_hash` + `args_hash`
- [x] `context.py` — RunContext, Lease, NodeScope
- [x] **T1** spec_hash determinism (cross-process, hash-seed independent)
- [x] **T10a** `ActorKind.HYBRID` raises `NotImplementedError` at spec compile

## PR-3 — Persistence

- [x] Migrations 001 foundations, 002 runs, 003 outbox
- [x] SQLAlchemy 2.0 models mirroring the DDL
- [x] Session factory, `UnitOfWork`, seven repositories
- [x] **T0** migrate up → down → up

## PR-4 — `RunService.start_run()`

- [x] Spec compile + `spec_hash` + immutable `RunSpec` snapshot into `run_specs`
- [x] ONE transaction: runs + run_specs + budget reservation + outbox + events
- [x] Idempotent on `(organization_id, idempotency_key)`
- [x] **T2** 100 concurrent identical keys → exactly 1 run row, 1 spec, 1 outbox,
      1 reservation

## PR-5 — Events

- [x] Outbox writer (in-transaction), relay loop → Redis Stream `XADD`
- [x] Lua-atomic publish dedupe; survives a stale script SHA after `FLUSHALL`
- [x] Consumer group, pending-entry recovery, `XAUTOCLAIM` of stale entries
- [x] **T3** relay crash mid-publish → each row published exactly once

## PR-6 — Worker pool

- [x] `LeaseManager`: claim (conditional UPDATE + fence bump), heartbeat, check
- [x] `Heartbeater` as its own task, so a long tool call cannot starve it
- [x] Reaper: expired leases → requeue + re-announce, `ABANDONED` at the cap
- [x] **T4** 5 workers, 1 run → exactly one claim
- [x] **T5** freeze → steal → thaw → `StaleFence` at gateway, zero effects

## PR-7 — First end-to-end run

- [x] `graphs/` registry; `echo_agent@1` (effect → respond)
- [x] Postgres LangGraph checkpointer in schema `lg` (migration 007)
- [x] `POST /v1/runs` → relay → worker → SUCCESS, asserted in `test_end_to_end.py`

## PR-8 — Effects

- [x] Migration 004: `effect_intents`, `sideeffect_fixture`, `audit_log`
- [x] `logical_call_id()` — pure function of five values, fence deliberately excluded
- [x] `EffectJournal`: begin / commit / fail / orphan, own transaction and connection
- [x] Recovery policies: `replay_safe`, `idempotency_key`, `probe`, `manual`
- [x] Capability entailment validation at registration
- [x] Blast-radius policy defaults (tighten-only, allow-list for approval)
- [x] `ToolGateway.execute()` full pipeline, fence checked before *and* after
- [x] `web.fetch@1` (replay_safe) · `fixture.sideeffect@1` (probe)
- [x] **T6** `logical_call_id` purity (property test + cross-process)
- [x] **T9** capability entailment; a bad tool fails registration
- [x] **T13** IRREVERSIBLE defaults → `retries=0`, `requires_approval=True`
- [x] **T7** kill during tool call ×50 × 2 durability modes → exactly one row

## PR-9 — Artifacts

- [x] Migration 005: artifacts, artifact_versions, artifact_links
- [x] `ArtifactStore` (S3/MinIO + filesystem dev backend), content-addressed
- [x] Fail-closed writes, including a post-write existence check
- [x] Tool results over the threshold → artifact, journal keeps the reference
- [x] **T12** store returns 500 → run FAILS, never reports success with no output

## PR-10 — Budget

- [x] Migration 006: budget_pools, budget_reservations, usage_ledger
- [x] reserve → reconcile → sweep; `ck_budget_invariant` is I8 in the DB
- [x] Admission holds released when a run reaches a terminal state
- [x] `ModelGateway.complete()` with mandatory `work_class` + `call_site`
- [x] **T11** 200 concurrent reservations on a pool sized for 100 → no overspend

## PR-11 — Deterministic workers

- [x] `handlers/` registry; `hasher@1` DETERMINISTIC_WORKER
- [x] `max_llm_calls == 0` enforced at `ModelGateway` entry
- [x] **T10** hasher model call raises; HYBRID raises at compile and at admission

## PR-12 — API + observability

- [x] `POST /v1/runs` (202), `GET /v1/runs/{id}`, `GET /v1/runs/{id}/stream` (SSE)
- [x] SSE backed by the `events` table, resumable with `?after=`
- [x] structlog binding the full ID set once per run, missing IDs rendered as null
- [x] OTel spans; `trace_id` present even when tracing is disabled
- [x] `test_observability.py` — the ID set is tested, not just asserted in prose:
      all seven present inside a run, explicit nulls outside one, no leakage
      between consecutive runs on the same worker

## PR-13 — Chaos harness

- [x] `tests/chaos_worker.py` — a worker in its own process, killable for real
- [x] `SIGKILL` between the effect landing and the journal recording it
- [x] **T8 / R1** `FLUSHALL` → recover: nothing lost, nothing doubled
- [x] Concurrent-worker and concurrent-relay races

---

## Definition of done

- [x] `alembic upgrade head` / `downgrade base` clean both directions
- [x] mypy strict clean · ruff check + format clean · **import-linter: 4 contracts kept**
- [x] T0–T13 green (142 tests)
- [x] T7 green ×50 × 2 durability modes, on 32 cores and pinned to 1 CPU
- [x] T8 (R1) green
- [x] Every log line carries the full ID set
- [x] Zero `TODO`s in `effects/` or `runtime/run_service.py`
- [~] `docker compose up` healthy < 60s — **written, not executed.** Docker is not
      installed on this machine; the compose job in CI is what proves it.
- [~] Both runners — verified on this 32-core host and again pinned to a single
      CPU with `taskset`. A `taskset` pin constrains parallelism but not memory or
      I/O bandwidth, so the genuine 1-vCPU runner is still the CI matrix's job.
- [~] `ARCHITECTURE.md` invariants verbatim — see below.

## Open before sign-off

1. **Replace `ARCHITECTURE.md` §2 with the verbatim v3 invariant text.** The v3
   document was not part of the implementation brief, so the fourteen statements
   there are reconstructed from how the plan cites them (I3/I4 gateways, I8 budget,
   I12 work class) and from what the code enforces. Each names the test that holds
   it, so a wording correction that changes the meaning will surface as a test that
   no longer fits.
2. **Run the compose job.** `docker-compose.yml` is written against the M0 spec but
   has never been started.
3. **Confirm the CI runner labels.** `ubuntu-latest-16-core` is a placeholder for
   whatever the org's large-runner label actually is.

## Harness notes

- **One test session per database.** Every test truncates every table before it
  runs and T0 downgrades to base, so two sessions sharing a database corrupt each
  other — and the resulting failures impersonate the exact bugs this suite exists
  to detect. A session-scoped `pg_advisory_lock` in `tests/conftest.py` makes the
  second session wait instead. Found the hard way: running the full suite
  concurrently with a chaos run produced four "exactly-once violations" that were
  nothing of the kind.
- The suite shells out via `sys.executable -m alembic`, so it does not require the
  virtualenv to be on `PATH`.

## Known limits, deliberately

- `RunContext.node()` takes a `checkpoint_ns`. `echo_agent@1` is linear so the empty
  namespace is correct, but **a graph with a cycle must pass the iteration index**
  or two loop iterations collide on one journal key. Documented at the call site.
- A tool that `requires_approval` cannot run at all in M0 — there is no approval
  subsystem, and refusing is the correct behaviour for an irreversible tool.
- `estimate_tool_cents()` returns 0 for every M0 tool. The reservation path is
  exercised anyway so that pricing a tool in M1 is a change to that one function.

## Not in M0

tasks · inbox · approvals · delegation · memory · triggers · cron · authority ·
evaluation · Context Engine · YAML · caching · second department · UI ·
multi-tenancy · HYBRID actors
