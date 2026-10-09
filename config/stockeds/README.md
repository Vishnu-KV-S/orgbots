# `config/stockeds` — the ed-tech trend desk

A one-actor company. `research` fires five times a day on five different ed-tech
beats and files a `CompetitorReport@1` each time; nothing drafts, nothing publishes,
and no manager plans the day.

    organization id: 63439f34-7cb8-4c1c-9d41-e69c0e797527

## The schedule

All times Asia/Kolkata, `catchup: skip`.

| | beat | what it is for |
|---|---|---|
| 07:00 | `edtech-funding-and-deals` | rounds, acquisitions, wind-downs, public-market moves |
| 11:00 | `edtech-ai-in-learning` | what shipped: tutoring, grading, assessment, curriculum, model releases |
| 14:00 | `edtech-policy-and-regulation` | AI-in-schools guidance, student data enforcement, procurement, programmes |
| 17:30 | `edtech-market-and-adoption` | adoption numbers, renewals, pricing, partnerships, earnings |
| 21:00 | `edtech-pedagogy-and-workforce` | efficacy studies, higher-ed shifts, reskilling and credentials |

Five triggers rather than one firing five times, because memory is off: the model has
no recollection of 07:00 when it runs at 14:00, so the **brief** is the only thing that
can make the slots differ. Each trigger carries its own `subject`, `objective` and
`acceptance_criteria`.

## Reading the reports

```bash
uv run python scripts/stockeds_digest.py 63439f34-7cb8-4c1c-9d41-e69c0e797527          # today, one paragraph each
uv run python scripts/stockeds_digest.py 63439f34-7cb8-4c1c-9d41-e69c0e797527 --full   # competitors, themes, sources
uv run python scripts/stockeds_digest.py 63439f34-7cb8-4c1c-9d41-e69c0e797527 --day 2026-09-01 --json
```

There is no task row behind a scheduled brief, so the report is **not** in `tasks` and
not in the weekly metrics. It lives in the run's `run.succeeded` event payload, which
is what the digest and the UI both read.

## What has to be running

```bash
uv run python -m runtime.worker.main    # the conductor lives here
```

`RUNTIME_CONDUCTOR_ENABLED` defaults to true, and it is the scheduler inside this
process that turns 17:30 into a run. With no worker up, the triggers are rows that
nothing evaluates and the day passes silently. The API process is not involved.

**A fresh trigger fires once on the first evaluation.** `Scheduler._evaluate` looks
back exactly one occurrence when `last_evaluated_at` is null, so starting a worker
against five newly applied triggers produces five immediate runs, one per beat,
regardless of the hour. That is the intended "do not send 47 plans on first boot"
behaviour and it costs about 25c; expect it once, after an `apply` that adds triggers.

## Two things that cost money to learn

- **`CompetitorReport@1` cross-checks its own names.** `_citations_resolve` rejects a
  theme naming an organization the `competitors` list does not cover. On the funding
  beat the model wrote one entry as `upGrad / Unacademy (acquisition closed)` and then
  referred to `upGrad` and `Unacademy` in its themes — the report failed validation on
  its own two spellings. Every brief now carries the naming rule as an acceptance
  criterion. It is not decoration.

- **`research@1`'s system prompt belongs to the M1 marketing department.** It tells the
  model that recommendations are "for a marketing team deciding what to say next week",
  and the first good report duly recommended building a social-proof asset. That prompt
  is shared with every company on this runtime, so the briefs redirect the field through
  the objective instead of editing it.

## Search

`web.search@1` has no endpoint (`RUNTIME_SEARCH_ENDPOINT_URL` is empty) and no stored
`search_api_key`, so the tool raises and `research@1` records a gap saying so. The
evidence comes from **DeepSeek's own server-side search** instead, which is offered
only to an actor already holding `web.search@1` — which is why that grant stays in
`40-governance.yaml` even though the tool behind it is unconfigured. Configuring the
endpoint adds the fetched pages as an evidence artifact; it does not switch the
provider search off.
