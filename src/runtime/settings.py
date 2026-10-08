"""Process configuration.

Read once at startup. Nothing here is consulted during a run — a run executes
under its frozen `RunSpec`, never under live config.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RUNTIME_", env_file=".env", extra="ignore", frozen=True
    )

    database_url: str = "postgresql+psycopg://runtime:runtime@127.0.0.1:5432/runtime"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    redis_url: str = "redis://127.0.0.1:6379/0"
    stream_prefix: str = "runtime"
    consumer_group: str = "workers"

    # Artifacts. `fs` is the dockerless dev backend; `s3` is MinIO or real S3.
    artifact_backend: Literal["s3", "fs"] = "s3"
    artifact_bucket: str = "runtime-artifacts"
    artifact_fs_root: str = ".devstack/artifacts"
    s3_endpoint_url: str | None = "http://127.0.0.1:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"  # noqa: S105  local MinIO default, not a secret
    s3_region: str = "us-east-1"

    # Anything larger than this in a tool result goes to the artifact store and the
    # journal keeps a reference. Keeps `effect_intents` rows small enough that the
    # journal transaction stays fast under contention.
    artifact_threshold_bytes: int = 32 * 1024

    # Leases.
    lease_seconds: float = Field(default=30.0, gt=0)
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    max_lease_expiries: int = Field(default=3, ge=1)
    reaper_interval_seconds: float = Field(default=5.0, gt=0)

    # Outbox relay.
    relay_batch_size: int = 100
    relay_interval_seconds: float = Field(default=0.25, gt=0)
    stream_maxlen: int = 100_000

    # Budget.
    reservation_ttl_seconds: float = Field(default=300.0, gt=0)
    budget_sweep_interval_seconds: float = Field(default=30.0, gt=0)

    # --- M1: the two tools that touch the world ------------------------------------
    # Both default to unset, and both *refuse* rather than degrade when unset. A
    # search tool that silently returns nothing produces a CompetitorReport with
    # invented sources, which is the single worst failure this milestone could have:
    # it looks like work, it passes schema validation if the agent is fluent enough,
    # and the only thing that would catch it is the 20% human sample.
    search_endpoint_url: str | None = None
    """A JSON search API taking `?q=` and returning `{"results": [{title,url,snippet}]}`."""
    search_api_key: str | None = None
    search_cents_per_call: int = 1
    """§ M0 retro: the first tool with a real price. `estimate_tool_cents` reads it."""

    publish_endpoint_url: str | None = None
    publish_api_key: str | None = None
    publish_target: Literal["staging", "production"] = "staging"
    """M1 risk 5: *"Point it at a staging destination for the first week regardless
    of what the approval gate says."* Defaulting to staging makes forgetting safe;
    production requires both this and `publish_allow_production`."""
    publish_allow_production: bool = False
    """A second, independent switch. One setting is a typo away from a real publish;
    two are not."""

    # --- model providers -------------------------------------------------------------
    # The *connection* lives here; the *model* does not. Which model an actor runs, at
    # what price and for which work class, is a `ModelProfile` frozen into its spec —
    # authored in the agents YAML below, hashed into `spec_hash`, and therefore
    # answerable months later from the run alone. An endpoint moved by an operator is
    # a deployment fact; a model swapped under a running department is a spec change,
    # and putting the two in the same file would blur that.
    deepseek_enabled: bool = True
    """On by default, because every profile in `runtime.org.department` now names it.
    It was off while DeepSeek was the optional second provider — a provider registered
    but unreachable is worse than an absent one — but with the department on DeepSeek
    the absent provider is the worse failure: `ModelCallNotAllowed: provider
    'deepseek' is not configured` at the first WORK call of a seeded department, for a
    setting nobody knew they had to turn on. On does not mean reachable; the
    credential is still resolved per call, and a missing one still names itself."""
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    """The Anthropic-compatible endpoint, not the OpenAI-compatible one — see
    `providers.deepseek_provider` for why that choice is load-bearing."""
    deepseek_api_key: str | None = None
    """The dev path. Production stores it in the M2 credentials table under
    `deepseek_api_key`, which is fetched per call and rotates without a restart.
    A bare `DEEPSEEK_API_KEY` in the environment also works — it is the SDK-style
    variable `DeepSeekProvider.env_var` names, read without the `RUNTIME_` prefix."""
    deepseek_web_search: bool = False
    """DeepSeek's *server-side* search: it runs the search, the tool gateway never
    sees it, and no `effect_intents` row is written. Off by default for that reason,
    and even on, the gateway offers it only to an actor holding `web.search@1`."""
    deepseek_web_search_tool: str = "web_search_20250305"
    deepseek_web_search_max_uses: int = Field(default=5, ge=1)

    # --- M4/M6: the config plane, over HTTP -----------------------------------------
    spec_roots: str = "config"
    """Comma-separated directories `/v1/control` may read and write spec files under.

    A `str`, not a `tuple[str, ...]`, and that is not laziness: pydantic-settings v2
    parses a complex-typed field from the environment as **JSON**, so
    `RUNTIME_SPEC_ROOTS=config` would raise rather than produce `("config",)`. The
    convention in this file is a comma-separated string — see `memory_injection_actors`
    — and `runtime.api.control.spec_roots()` is the one place that splits it.

    Every path the control surface touches is resolved against these roots, with
    symlinks followed *before* the comparison, and refused if it lands outside. The
    roots are a confinement boundary, not a search path."""

    spec_editable: bool = True
    """Whether `/v1/control` may write spec files at all.

    The blunt instrument. `/v1/control` has no authentication of any kind: anything
    that can reach it can stop a department, spend money and rewrite `config/`. That is
    fine on a local devstack and must not be exposed on a network. Set this to `false`
    and every write verb answers 403 while the reads keep working."""

    spec_operator: str = "ui"
    """The name recorded as `created_by` / `engaged_by` / `applied_by` for actions taken
    through `/v1/control`.

    There is no auth, so there is no caller identity to draw on. Recording a constant
    that says *where* the action came from is honest; inventing a person would not be,
    and `applied_by = "operator"` for both a CLI apply and a UI apply would make the
    apply history unable to answer the one question it exists for."""

    # --- agents ----------------------------------------------------------------------
    agents_config_path: str | None = None
    """Optional YAML overlay for the department's model profiles. Unset means the
    values in `runtime.org.department` — which stay the defaults, so a checkout with
    no config file behaves exactly as it did. See `config/agents.example.yaml`."""

    # --- M1: scheduler and dispatcher ----------------------------------------------
    scheduler_interval_seconds: float = Field(default=20.0, gt=0)
    dispatcher_interval_seconds: float = Field(default=1.0, gt=0)
    dispatcher_batch_size: int = Field(default=16, ge=1)
    approval_sweep_interval_seconds: float = Field(default=60.0, gt=0)
    eval_deadline_sweep_interval_seconds: float = Field(default=60.0, gt=0)

    conductor_enabled: bool = True
    """Whether `runtime.worker.main` runs the scheduler and the dispatcher.

    **On by default, and that is the point.** Without it there is no long-lived
    process that turns a cron into a run or an inbox message into a run — only
    `runtime.cli tick`, typed by a human. `docs/MEASUREMENT_PROTOCOL.md` §4 requires
    two consecutive weeks with the long-lived processes left alone and counts any
    intervention as a reset, so a runtime with no conductor cannot *have* a clean run:
    somebody has to type `tick`, and typing it is the intervention.

    Turn it off (`RUNTIME_CONDUCTOR_ENABLED=false`) when something else is driving the
    loop — a test that ticks deliberately, a second deployment that owns scheduling,
    or a worker pool scaled out behind one conductor. Two conductors are safe (the
    `trigger_fires` primary key and `uq_run_idem` make them idempotent); zero is not.

    The scheduler half also needs `scheduler_enabled`, which is off by default."""

    scheduler_enabled: bool = False
    """Whether the conductor's scheduler fires cron triggers by itself.

    **Off by default: work starts on an instruction, not on a clock.** A run is started
    by a person (a chat message, the run button, `tick`), or by the run that delegated
    to it (`ask_bot`, a head assigning a task through the inbox — which is why the
    dispatcher stays on). A trigger nobody is watching otherwise starts research runs on
    its own, and after any downtime catches up with one run per trigger at startup.

    `runtime.cli tick` and the UI's tick still fire due triggers: pressing it is the
    instruction. Set `RUNTIME_SCHEDULER_ENABLED=true` for a department that is meant to
    run itself — §4's clean run needs it, since there a person typing `tick` is the
    intervention."""

    # --- M3: memory ------------------------------------------------------------------
    # Everything here is off or shadowed by default. M3 §9: PR-27 through PR-34 are safe
    # to build at any time because none of them touches a prompt, and **PR-35 is the only
    # one that changes what the models see**. The default value of
    # `memory_injection_enabled` is what makes that sentence true of a checkout as well
    # as of a plan.

    memory_enabled: bool = False
    """Master switch for the whole subsystem: write path, retrieval, traces.

    Off by default so that M1's and M2's measurement weeks can be re-run from this
    checkout and produce comparable numbers. Turning it on with
    `memory_injection_enabled` still off is shadow mode (§5), which is where M3 is
    supposed to live for its first two weeks."""

    memory_injection_enabled: bool = False
    """**PR-35.** Whether retrieved memories actually enter a prompt.

    Separate from `memory_enabled` because the two answer different questions and §5's
    entire argument is that they must be flippable independently: *"Injecting first and
    measuring later means quality and cost move together and you can attribute
    neither."* Per-actor overrides live in `memory_injection_actors`."""

    memory_injection_actors: str = ""
    """Comma-separated actor names, applied when `memory_injection_enabled` is on.

    §13 risk 1 names the per-actor flag as the escape hatch. Empty means every actor;
    naming actors means only those, which is how injection is rolled out to one actor
    for a week before the department."""

    memory_shadow_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """Fraction of eligible calls that retrieve at all. 1.0 in shadow mode — the whole
    point is to accumulate traces to grade — and a dial for later, when retrieval is
    live and the question is cost rather than evidence."""

    memory_top_k: int = Field(default=20, ge=1)
    """`[CHOSEN]` §7. Retrieved from the store, before reranking."""
    memory_inject_k: int = Field(default=6, ge=0)
    """`[CHOSEN]` §7's `K=3-6`, at the top of the range."""
    memory_max_injected_tokens: int = Field(default=900, ge=0)
    """`[CHOSEN]`. Replace with the budget eval 8 produces; until then it is a stop,
    not a budget."""
    memory_min_similarity: float = Field(default=0.15, ge=0.0, le=1.0)
    """`[CHOSEN]`. Below this a candidate is noise the reranker would otherwise dress
    up with recency and importance. A floor on the *similarity* term specifically,
    because that is the only term that says the memory has anything to do with the
    query."""

    memory_rerank_similarity: float = 1.0
    memory_rerank_importance: float = 0.3
    memory_rerank_recency: float = 0.2
    memory_rerank_access: float = 0.1
    memory_rerank_half_life_days: float = Field(default=30.0, gt=0)
    """The four rerank weights and the decay, in config rather than buried in code —
    §7 asks for exactly this, so that tuning shows up in a diff."""

    memory_store: Literal["native", "mem0"] = "native"
    """Which `MemoryStore` adapter to construct.

    `native` is the default because pgvector is absent from stock Postgres and from
    this repository's dev stack (`docs/M3_LIBRARY_FACTS.md` fact 3), and gating the
    three zero-tolerance isolation evals on an optional package would make them
    decorative. `mem0` is the production path and needs `pip install
    'agent-org-runtime[memory]'` plus the extension."""

    memory_consolidation_enabled: bool = True
    """The consolidation worker. Always off the hot path (§2), so this is about whether
    it runs at all, never about where."""
    memory_worker_interval_seconds: float = Field(default=5.0, gt=0)
    memory_write_batch_size: int = Field(default=16, ge=1)

    memory_prune_after_days: float = Field(default=180.0, gt=0)
    """`[CHOSEN]` §13 risk 2. A memory this old that has never once been retrieved is
    retired — not deleted, so the pruning stays measurable."""

    memory_procedure_min_observations: int = Field(default=3, ge=2)
    memory_procedure_min_successes: int = Field(default=2, ge=1)
    """`[CHOSEN]`. Two observations is a coincidence with a sample size; three with two
    successes is the smallest thing worth a reviewer's attention. Both are a starting
    position and neither is derived from anything."""

    # --- M3: embeddings --------------------------------------------------------------
    # Same rule as M1's two world-touching tools: unset means the deterministic local
    # embedder, and it says so in a log line rather than degrading quietly.
    embedding_endpoint_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str = "hashing-v1"
    embedding_dim: int = Field(default=256, ge=8)
    embedding_cents_per_mtok: int = 0
    embedding_version: str | None = None
    """Overrides the derived `provider-model-dim` collection name (§3.3). Set it when a
    provider reissues a model under an unchanged name — which is the exact failure the
    version string exists to survive, and the only one the derived name cannot see."""

    # --- M5: delegation ---------------------------------------------------------------
    # Off by default, and that default is what M5a's exit criterion is written over:
    # *"a checkout with M5a merged and delegation off is indistinguishable from M4"*
    # (T68). Everything below this line is dark until somebody sets one environment
    # variable, which is the same shape M3 shipped in and for the same reason — the
    # code being present is not the same claim as the code being on.

    delegation_enabled: bool = False
    """Master switch. `delegate()` refuses with `DelegationDisabled` when it is off,
    loudly rather than by degrading to doing the work inline: a flag that changed what
    a graph *did* rather than what it *could* do would make M4's and M5's numbers
    incomparable, which is the one thing the dark ship is protecting."""

    delegation_max_depth: int = Field(default=2, ge=0, le=8)
    """`[CHOSEN]` §2 puts *delegation depth > 2* out of scope, so this is that scope as
    a number. It is the **ceiling on the per-actor limit**, not a replacement for it:
    an actor's own `maxDepth` still governs, and this is what stops a configuration
    edit from opting the organization into depth 5 without a code change.

    The upper bound of 8 is not a considered limit, it is a guard: the descendant walk
    is a recursive query and a depth nobody reviewed is a query nobody bounded."""

    delegation_root_pool_enabled: bool = True
    """The fourth budget level (`root_run` scope), which M2 built and left off.

    Separate from `delegation_enabled` so that T55 can run the chain-reservation
    concurrency test in **both** configurations against the same code — turning the
    root pool on changes the lock chain from depth 3 to depth 4 and adds pool
    *creation* under contention to a path that previously only did pool *update*, and
    that is a different concurrency shape rather than the same one with a longer chain.

    Only consulted when delegation is on. A run tree with one member gains nothing from
    a pool that bounds what `ceilings.max_cost_cents` already bounds, and pays a row and
    a lock per reservation for it."""

    delegation_child_poll_seconds: float = Field(default=0.25, gt=0)
    """How often a waiting parent asks whether its child has finished.

    A poll rather than a notification, and that is a deliberate M5a simplification: the
    parent holds its lease and heartbeats while it waits, so a crash is the ordinary
    lease-expiry case the reaper already handles (T66) rather than a new suspend/resume
    path with its own failure modes. The cost is a worker slot held for the child's
    duration, which is the right trade at depth 2 and the wrong one at depth 5 — which
    is another reason depth 5 is out of scope."""

    delegation_child_timeout_seconds: float = Field(default=900.0, gt=0)
    """How long a parent waits before giving up on a child and cancelling it.

    Not the child's deadline — the child has its own — but a backstop for the parent:
    a child whose worker died and whose lease is being retried can outlive any
    reasonable parent, and a parent that waited forever would hold a lease forever and
    be reaped, releasing nothing. On expiry the parent cancels the subtree and reports
    what it got, which is `drain` semantics arrived at by the clock."""

    worker_slots: int = Field(default=1, ge=1, le=64)
    """How many runs one worker process executes at once — that many independent
    worker loops, each with its own id, each claiming runs the ordinary way (I5's
    conditional claim is what makes several in one process as safe as several
    processes). One is the M0 default and what the chaos tests assume.

    **Bots want at least 2**, and a few more is better: every bot that is working holds
    a slot, and a bot that asks a helper (`ask_bot`) holds its slot *while the helper
    works in another one* — with a single slot the two would wait on each other until
    the delegation timeout."""

    # --- bots ------------------------------------------------------------------------
    computer_url: str = "http://127.0.0.1:8020"
    """The shared browser (`python -m runtime.computer.main`). The browser tools and the
    API's watch/take-control proxy both talk to it; nothing else does."""

    bots_organization_id: str = "00000000-0000-4000-8000-0000000000b0"
    """The organization personal bots live in when a request names none. Bots are
    actors, so they need an organization; this one is created on first use."""

    bots_organization_name: str = "Personal"

    bot_auto_continue_chunks: int = Field(default=5, ge=1, le=20)
    """How many runs ("chunks") one instruction to a bot may take before it stops and
    asks for "continue". A run is `MAX_STEPS` (24) steps; past that, a long task posts
    itself a `bot.continue` message and the dispatcher starts the next chunk with the
    same turn, so Stop and a new message still interrupt it. 1 means never carry on
    unasked.

    **The dispatcher does this**, so it only happens where the conductor runs
    (`conductor_enabled`); with the conductor off, every chunk ends with "say continue",
    as it did before. **Worst case it is this many times a run's ceiling** — 5 x 300
    cents, $15, for one instruction — though a step costs about a cent."""

    public_url: str = ""
    """Where this API is reachable from outside, e.g. a tunnel's https URL. Used only to
    show an event routine's webhook URL; unset, the URL is built from the request, which
    is right for a sender on the same machine and wrong for GitHub or Slack."""

    push_contact: str = "mailto:bots@localhost"
    """Who runs this runtime, for the push services (Web Push's VAPID `sub` claim). Some
    services reject a push without one; set a real `mailto:` or `https:` address."""

    bot_routines_enabled: bool = True
    """Whether the worker starts bots' routines (`runtime.runtime.routines`).

    On by default, unlike `scheduler_enabled`, because a routine exists only when a
    person created one, or asked their bot for it in the conversation: it is their
    standing instruction, not the department's clock. It still runs only where the
    conductor runs (`conductor_enabled`). A routine waits for a busy bot rather than
    interrupting it, and a worker that was down fires each routine's latest missed
    occurrence once, never the backlog."""

    credential_keys: str = ""
    """`RUNTIME_CREDENTIAL_KEYS`, read here so `.env` is enough for the login vault
    (`runtime.gateway.vault.load_cipher`). The credential broker still reads the process
    environment only; this copy fills in for the vault when the environment lacks it."""

    credential_active_key: str = ""

    log_level: str = "INFO"
    log_json: bool = True
    otel_enabled: bool = False
    service_name: str = "agent-org-runtime"

    @property
    def sync_database_url(self) -> str:
        """Alembic and the LangGraph checkpointer setup path want a sync DSN."""
        return self.database_url.replace("+psycopg_async", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
