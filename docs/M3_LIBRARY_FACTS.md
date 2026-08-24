# M3 §3 — library facts, verified

Every claim in M3 §3 is tagged `[VERIFY]` and says: *"Confirm each against current
documentation and record the version and date you checked."* This is that record.

| | |
|---|---|
| Checked on | **2026-08-23** |
| `mem0ai` | **2.0.18** (installed from PyPI, source read) |
| Postgres | 18.x (`scripts/devstack.sh` cluster), 16-alpine (`docker-compose.yml`) |
| pgvector | **not available on this machine** — see fact 3 |

Re-run this check on every `mem0ai` upgrade and update the table. Facts 3 and 4 are
the two that decide whether the isolation tests still mean what they meant.

---

## 1. Mem0 is an extraction and fusion layer, not a vector store — CONFIRMED

`AsyncMemory.__init__` (`mem0/memory/main.py:2166`) constructs three separate things:

```python
self.embedding_model = EmbedderFactory.create(...)
self.vector_store    = VectorStoreFactory.create(...)
self.llm             = LlmFactory.create(...)
```

The vector store is a dependency it drives, not something it replaces. `_add_to_vector_store`
searches for similar existing memories, hands them to `self.llm.generate_response` with an
update prompt, and applies the returned `ADD`/`UPDATE`/`DELETE`/`NONE` events.

**Consequence for us — §3.1 says "test theirs before building yours."** We do not write a
supersession *engine*. `memory_metadata.supersedes` / `superseded_by` are written from the
events Mem0 emits, and exist for audit — which is exactly what §3.1 predicted we would need
them for. `NativeMemoryStore` implements the same four-event contract so the sidecar has one
shape to record regardless of which store is underneath.

## 2. Asynchronous writes — CONFIRMED, with a caveat that changed our design

`AsyncMemory` is real, and every synchronous call inside it is wrapped:

```python
response = await asyncio.to_thread(self.llm.generate_response, ...)
msg_embeddings = await asyncio.to_thread(self.embedding_model.embed, msg_content, "add")
```

The write path runs a *search*, then an *LLM call*, then embeddings, then inserts. It is
several model round-trips per run. It belongs in a worker, which is where
`runtime.memory.worker.MemoryWorker` puts it.

**The caveat is the useful part.** Because Mem0 calls its LLM and embedder from a
`asyncio.to_thread` worker thread, an adapter can bridge back to the runtime's event loop
with `asyncio.run_coroutine_threadsafe(...).result()` without deadlocking — the calling
thread is never the loop thread. That is the mechanism that lets Mem0's extraction run
through `ModelGateway` instead of through a client it owns. See fact 7.

## 3. pgvector dimension is fixed at first write — CONFIRMED, and it is not installed here

`mem0/vector_stores/pgvector.py` creates the collection table with `vector(%s)` at the
configured `embedding_model_dims`. Changing embedder afterwards inserts a differently-sized
vector into a fixed-width column and Postgres refuses it.

```
$ psql -c "select * from pg_available_extensions where name like '%vector%'"
(0 rows)
```

Neither the devstack Postgres 18 cluster nor `postgres:16-alpine` ships pgvector. Installing
it is a package install with root (`postgresql-18-pgvector`) or a different base image.

**This is why `MemoryStore` is a port with two adapters** — see `ARCHITECTURE.md` §7 and
`src/runtime/memory/store.py`. `NativeMemoryStore` stores vectors as `real[]` and does exact
cosine in SQL; it needs no extension, so **T45–T54 and evals 4, 5 and 9 run in CI on a stock
Postgres**. The hard gates are the whole point of M3 and gating them on an extension nobody
has installed would have made them decorative.

`NativeMemoryStore` enforces the dimension *itself*, from `mem.collections.dim`, even though
`real[]` would not require it. That is deliberate: T45 has to test a real constraint, and an
adapter that quietly accepted a re-dimensioned vector would pass a test that pgvector fails.

## 4. Scope filter correctness — CONFIRMED FIXED in 2.0.18, and we still do not rely on it

Both fixes §3.4 describes are present in `_build_filter_conditions`:

```python
if op in ("in", "nin"):
    if not isinstance(op_value, list):
        raise ValueError(f"Filter operator {op!r} for key {key!r} requires a list value, ...")
...
elif op in ("contains", "icontains"):
    escaped = str(op_value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions.append(template + " ESCAPE '\\'")
```

So the string-iterated-as-a-list bug and the LIKE-metacharacter bypass are both closed in
this release.

**We do not treat that as the isolation boundary.** §13 risk 3: *"Scope filtering is a
security boundary implemented by a third-party library."* The authoritative filter is ours —
`memory_metadata` is joined in the retrieval query and the `(organization_id, scope,
scope_id, status, trust)` predicate is applied in our SQL, parameterised. The store's filter
is passed too, as defence in depth and as a performance hint. A regression in the library
therefore costs recall, not isolation. T46 and T47 test our validator; T48 and T49 test the
end-to-end property against both adapters.

## 5. Telemetry — CONFIRMED, and it does more than log

`mem0/memory/telemetry.py:14` — `MEM0_TELEMETRY = os.environ.get("MEM0_TELEMETRY", "True")`,
so it is **on unless you turn it off**. There is also `MEM0_TELEMETRY_SAMPLE_RATE`.

Worth knowing beyond the notices: when telemetry is on, `AsyncMemory.__init__` builds a
*second vector store* against a `mem0migrations` collection. On pgvector that is an extra
table in your database, created at construction.

`Mem0MemoryStore` sets `MEM0_TELEMETRY=False` in the process environment before importing
`mem0`, and `test_m3_store.py::test_telemetry_is_disabled_before_the_import` asserts it,
because setting it after the import is too late — the module reads it at import time.

## 6. Cross-session identity, temporal abstraction, staleness — unchanged, still open

Nothing here to verify. Evals 1, 2, 6 and 7 are how we find out when they bite, and eval 2 is
on a schedule (§13 risk 2) rather than run once.

## 7. Extra fact, not in §3: Mem0 owns its own LLM, embedder, connection pool and history DB

This one is not in the plan and it is the one that shaped the most code, so it is recorded
here rather than discovered again later.

`AsyncMemory.__init__` also does `self.db = SQLiteManager(self.config.history_db_path)` and
the pgvector store opens its own `psycopg_pool.ConnectionPool`. Left alone, an M3 built
straight on Mem0 would have:

- model calls that never reach `ModelGateway` — **no `work_class`, so I12 is violated and
  §13 risk 4's "tag every extraction call `work_class=MEMORY`" is impossible**; and no budget
  reservation, no kill switch, no rate limit, no audit row;
- credentials resolved from the environment rather than the M2 credentials table, undoing T37;
- a SQLite file holding history next to a Postgres cluster that holds everything else.

The injection points are ordinary class attributes — **and the two registries do not have
the same value shape**:

```python
LlmFactory.provider_to_class["openai"]       # ("mem0.llms.openai.OpenAILLM", OpenAIConfig)
EmbedderFactory.provider_to_class["openai"]  # "mem0.embeddings.openai.OpenAIEmbedding"
```

A tuple in one, a bare string in the other. Registering a tuple in the embedder registry
does **not** fail at registration; it fails later, inside `EmbedderFactory.create`, as:

```
AttributeError: 'tuple' object has no attribute 'rsplit'
```

which names neither this project nor the mistake. That is what happened on the first run
against the real library. `_assert_registry_shapes` now compares our two entries against
Mem0's own `openai` entries at startup, so an upgrade that changes the convention is
caught by *the convention changing* rather than by our guess about it going stale.

`runtime.memory.mem0_store` registers `gateway` entries in both, pointing at adapters that
call `ModelGateway` and `EmbeddingGateway` over the loop bridge from fact 2. Mem0 keeps the
extraction and fusion — which is what we wanted from it — and the runtime keeps the
accounting, which is what it is not allowed to give away.

Verified on 2026-08-23 by constructing both through the factories' own `create()`:

```
llm      -> runtime.memory.mem0_store.GatewayLLM
embedder -> runtime.memory.mem0_store.GatewayEmbedder
MEM0_TELEMETRY: False
```

The history DB is pointed at a temporary path and ignored; `memory_audit` is ours.
