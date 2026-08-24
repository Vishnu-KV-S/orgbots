## What this changes

<!-- One paragraph. What is different after this merges? -->

## Which invariants does it touch?

The fourteen invariants are in `ARCHITECTURE.md` §2. Tick every one this change
could affect — including ones it *preserves* by design, not only ones it modifies.
If none, say so explicitly rather than leaving the section blank.

- [ ] I1 — one door: runs are created only by `RunService.start_run()`
- [ ] I2 — `logical_call_id` is a pure function of its five inputs
- [ ] I3 — every external call goes through a gateway
- [ ] I4 — gateways own credentials, retries and audit
- [ ] I5 — one run, one owner
- [ ] I6 — the fence is checkable after the fact
- [ ] I7 — intent is durable before the effect
- [ ] I8 — budget cannot be exceeded
- [ ] I9 — announcements are transactional
- [ ] I10 — Redis is reconstructible
- [ ] I11 — a run executes under a frozen spec
- [ ] I12 — every model call declares its work class
- [ ] I13 — a recovery policy must be entailed by capabilities
- [ ] I14 — blast radius sets policy; a tool may only tighten it
- [ ] None of the above

For each ticked box, name the test that still holds it.

## Checklist

- [ ] `ruff check .` and `mypy` clean
- [ ] `lint-imports` — all four contracts kept
- [ ] Migrations tested up **and** down (T0)
- [ ] If this touches `effects/` or `runtime/run_service.py`: no new `TODO`s
- [ ] If this touches the tool gateway or the journal: T7 run locally at
      `RUNTIME_CHAOS_ITERATIONS=50` on both durability modes
