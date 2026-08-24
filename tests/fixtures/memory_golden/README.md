# The memory golden set

**What is in this directory is not the golden set.** Read this before quoting any
number from `runtime.cli memory evals`.

M3 §10 is explicit about where the corpus comes from:

> *"Build it from data you already have. M1 and M2 produce weeks of real runs, artifacts
> and evaluated tasks — that is the corpus. **Do not write synthetic facts**; the point
> is that these are things your actors actually encountered."*

That instruction splits the five files in two, and only one half can live in a git
repository.

| file | kind | ships here | drives |
|---|---|---|---|
| `facts.jsonl` | real | **no** | eval 1 (fact recall) |
| `queries.jsonl` | real, hand-labelled | **no** | eval 2 (retrieval precision) |
| `contradictions.jsonl` | real | **no** | eval 6 (contradiction handling) |
| `leakage.jsonl` | synthetic probe | yes | evals 4 and 5, T48/T49 |
| `quarantine.jsonl` | synthetic probe | yes | eval 9, T50/T52 |

## Why two of them are synthetic and that is correct

Evals 4, 5 and 9 are **isolation** properties with zero-tolerance gates. An isolation
probe does not care whether its content is realistic; it cares that a scoped fact and a
foreign-scope query exist and that the query returns nothing. Synthetic is not a
compromise there — it is better, because you can construct the adversarial case directly
instead of hoping a real week happened to contain one.

`quarantine.jsonl` is keyed by the **M2 injection corpus** payload names, so eval 9 and
`docs/INJECTION_RESULTS.md` are talking about the same payloads. Note that
`05_memory_seed.txt` exists in that corpus with `attack: memory_write` — M2 wrote a
memory-poisoning payload before there was a memory to poison, and it is the one to look
at first when eval 9 moves.

## Why three of them are not here, and what to do about it

`facts.jsonl`, `queries.jsonl` and `contradictions.jsonl` are **quality** measurements. A
synthetic fact measures how well retrieval works on sentences somebody wrote to be
retrievable, which is a number about the author. Build them from your own runs:

```bash
uv run python -m runtime.cli --organization $ORG memory golden --build
```

That writes all three from `memory_metadata`, `context_traces` and the run history.
`contradictions.jsonl` comes out complete — a supersession pair is a pair because the
store already decided it was. The other two come out **unlabelled**, and the labelling
is the day of work §10 budgets for:

- `facts.jsonl` needs no labels, but it is only meaningful once there are weeks of runs
  behind it. It is filtered to facts that *recurred*, because eval 1 is really asking
  about recall on the facts that mattered.
- `queries.jsonl` ships every row with `expected_memory_ids: []` and a `TODO` marker.
  Someone has to read each query and each candidate and decide which ones *should* have
  returned. **`EvalSuite` refuses to score eval 2 over unlabelled rows** rather than
  reporting 0% or 100% depending on which way the arithmetic falls.

§13 risk 5 is that this gets skipped: *"Unglamorous, no visible output, and without it
every number in §11 is a guess with a decimal point."*

## PROVENANCE

The `PROVENANCE` file in this directory is read by `runtime.memory.golden.load()` and
its value is attached to every `EvalResult`. It says `placeholder` here. Change it to
`real` only when the three built files hold data from your own runs and
`queries.jsonl` has been labelled — because "we scored 84%" and "we scored 84% on a
corpus nobody labelled" need to be different strings when they reach a status meeting.
