# `config/org` — the organization, as documents

This directory is the M1 marketing department expressed in YAML. It was produced by

    python -m runtime.cli spec export --out -

and split by kind for review; the loader reads the whole directory, sorted, so the file
names are for humans and the split is not load-bearing.

**It compiles to exactly what `runtime.org.department` produces.** Every actor's
`spec_hash` here is byte-identical to the hash the Python definitions produce, and
`tests/test_m4_roundtrip.py` asserts it on every CI run. That test is the entire claim
M4 makes: the config plane changed nothing about how the system behaves.

## Working with it

    python -m runtime.cli spec validate                       # no database needed
    python -m runtime.cli --organization $ORG spec plan
    python -m runtime.cli --organization $ORG spec apply --plan $PLAN_ID
    python -m runtime.cli --organization $ORG spec drift

`validate` is the one to run in CI. `plan` is meant to be read like a code review —
M4 §13's first risk is a quiet bad apply, and the diff is the guard.

## Rules the format enforces

- **Secrets are never inline.** `credentials: {secretRef: search_api_key}` points at
  the encrypted store (`runtime.cli credentials --put`). A literal string under any
  credential-shaped key is a validation error, not a warning.
- **References are version-pinned.** `web.search@1`, `research@1`. An unpinned
  reference is refused, because RunSpec pinning is what makes "which version did this
  run use" answerable.
- **One kind per document**, several documents per file, separated by `---`.

## What editing this does *not* do

Nothing, until you run `apply`. And an `apply` does not touch runs already in flight:
a run executes under the RunSpec it was admitted with, so the earliest a change can
reach anything is the next run that starts.

**Do not run `apply` during a measurement window.** M4 §12 makes this an operational
rule rather than a mechanism, deliberately: the tool makes the org easy to change, and
a two-week clean run means two weeks without changes.
