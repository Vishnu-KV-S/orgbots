# LangGraph / LangChain mechanisms for long-running agents (as of Oct 2026)

Method note: besides the docs, I downloaded and read the current PyPI wheels on 2026-10-09: `langgraph` 1.2.14, `langgraph-checkpoint` 4.2.0, `langgraph-prebuilt` 1.1.0, `langgraph-sdk` 0.4.6, `langchain` 1.4.4, `langchain-classic` 1.0.8, `deepagents` 0.7.23, `langmem` 0.0.30. Statements marked "(source)" come from that code. Repo links point at the matching paths in the GitHub repos (https://github.com/langchain-ai/langgraph, /langchain, /deepagents, /langmem). The docs moved from langchain-ai.github.io/langgraph to docs.langchain.com/oss/python/...

## 1. recursion_limit, RemainingSteps / IsLastStep, graceful wrap-up

### Takeaway
`recursion_limit` caps the number of **supersteps per invocation**, not the total for a thread. It is no longer a small cap. The docs say the default became 1000 in 1.0.6, but the shipped 1.2.14 code sets 10007 (env-overridable), and `create_agent` and `create_deep_agent` both pin 9_999. LangGraph's intended approach is to put the `RemainingSteps` / `IsLastStep` managed values in state and route to a wrap-up node before the limit, so the run finishes cleanly with a checkpoint instead of raising `GraphRecursionError`.

### Cited Findings
- Definition: "The recursion limit sets the maximum number of super-steps the graph can execute during a single execution." The step counter is in `config["metadata"]["langgraph_step"]`. Exceeding the limit raises `GraphRecursionError`. — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api)
- Docs: "Starting in version 1.0.6, the default recursion limit is set to 1000 steps." (Historically it was 25.) — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api). **Conflict:** the 1.2.14 source has `DEFAULT_RECURSION_LIMIT = int(getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007"))` — [langgraph `_internal/_config.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/_internal/_config.py) (source). `errors.py` still shows `{"recursion_limit": 1000}` as its example.
- You set it as a top-level config key, not inside `configurable`: `graph.invoke(inputs, config={"recursion_limit": 5})`. The value must be ≥1 (`ValueError("recursion_limit must be at least 1")`). — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api); [pregel/main.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/main.py) (source)
- How it counts (source): on each invocation the loop sets `self.stop = self.step + self.config["recursion_limit"] + 1`. Each `tick()` checks `if self.step > self.stop: self.status = "out_of_steps"`. `main.py` then raises `GraphRecursionError(f"Recursion limit of {limit} reached without hitting a stop condition. You can increase the limit by setting the recursion_limit config key.")`. The limit is relative to the step the run **starts** from, so resuming a thread (invoke with `None`/`Command`) gets a fresh budget of supersteps. — [pregel/_loop.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_loop.py) (source)
- Managed values (source, `langgraph/managed/is_last_step.py`):
  - `IsLastStep = Annotated[bool, IsLastStepManager]`, which returns `scratchpad.step == scratchpad.stop - 1`.
  - `RemainingSteps = Annotated[int, RemainingStepsManager]`, which returns `scratchpad.stop - scratchpad.step`.
  - Import from `langgraph.managed`. Declare as `remaining_steps: RemainingSteps` in the state schema. LangGraph fills it in and does not persist it. — [is_last_step.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/managed/is_last_step.py)
- The docs recommend the proactive pattern: check `state["remaining_steps"]` in a node or router and, e.g. when `remaining <= 2`, route to a `fallback_node`/`END`. The graph then "completes normally", saves intermediate state in checkpoints and returns partial results. The reactive alternative is `try/except GraphRecursionError`, which terminates execution. — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api)
- How the legacy prebuilt uses it: `create_react_agent`'s `AgentState` declares `remaining_steps: RemainingSteps`. If `remaining_steps < 2 and has_tool_calls`, it replaces the response with "Sorry, need more steps to process this request." `create_react_agent` is deprecated with `LangGraphDeprecatedSinceV10`: "create_react_agent has been moved to `langchain.agents`... `from langchain.agents import create_agent`." — [chat_agent_executor.py](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py) (source, langgraph-prebuilt 1.1.0)
- LangChain v1 `create_agent` hard-sets `config = {"recursion_limit": 9_999}` with the comment "Set recursion limit to 9_999 — https://github.com/langchain-ai/langgraph/issues/7313". Deep Agents does the same: `.with_config({"recursion_limit": 9_999, ...})`. In practice, step bounding for these agents has moved to middleware such as `ModelCallLimitMiddleware` and `ToolCallLimitMiddleware` (section 5). — [langchain agents/factory.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py); [deepagents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py) (source)

### Inferences
- The bot's "24 passes per run" is a self-imposed design limit, not a LangGraph limit. Current LangGraph defaults allow about 1k–10k supersteps per invocation. A look/act cycle that is two nodes uses 2 supersteps per pass.
- The bot's chunking (end the run, post a message, start a fresh run) closely matches what LangGraph does natively when you **resume the same thread**. Each new invocation on the same `thread_id` gets a new recursion budget and starts from the last checkpoint. "Chunks" can therefore just be repeated `invoke(None or Command(...), {"thread_id": same})` calls. The carry-over step log, plan and notes can live in graph state rather than being re-injected as a message.
- Use `RemainingSteps` (or a custom counter in state) to route to a "summarize progress / write plan" node 2–3 steps before the per-run budget runs out. That gives a graceful handoff instead of a hard stop.

### Gaps
- I could not find a changelog entry explaining the docs-vs-source discrepancy (1000 vs 10007). Issue #7313 was not fetched.

## 2. Durable execution: checkpointers, resume, durability modes, Functional API

### Takeaway
With a checkpointer (e.g. `PostgresSaver`), LangGraph saves state at every superstep boundary. It stores per-task "pending writes" so completed sibling nodes aren't re-run, and it resumes a crashed or limited thread when you call it again with `None` (or `Command(resume=...)` after an interrupt) and the same `thread_id`. The `durability` argument (`"sync"`, `"async"` default, `"exit"`) trades latency against crash safety. The Functional API (`@entrypoint` / `@task`) gives the same guarantees at task granularity.

### Cited Findings
- Durability modes (source docstring): `Durability = Literal["sync", "async", "exit"]`.
  - `'sync'`: "Changes are persisted synchronously before the next step starts."
  - `'async'`: "persisted asynchronously while the next step executes."
  - `'exit'`: "persisted only when the graph exits."
  - The default resolves to `config[CONF].get(CONFIG_KEY_DURABILITY, "async")`, i.e. **"async"**.
  - Pass per call, e.g. `graph.stream(inputs, durability="sync")`.
  - Source: [types.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py), [pregel/main.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/main.py); [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- Per the docs, `"exit"` means mid-run crashes cannot be recovered, and `"async"` carries "a small risk of missing checkpoints if the process crashes". The SDK deprecates `checkpoint_during` in favor of `durability`. — [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers); langgraph-sdk 0.4.6 `_async/runs.py` (source)
- Pending writes: "LangGraph stores pending checkpoint writes from any other nodes that completed successfully at that super-step." On resume, successful nodes are not re-run. — [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- On retry or resume, "the affected node runs again from the start of its function", so node logic should be idempotent. — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api)
- Postgres setup:
  - `from langgraph.checkpoint.postgres import PostgresSaver`, then `PostgresSaver.from_conn_string("postgresql://...")` and `checkpointer.setup()  # Creates tables with indexes`.
  - Use `AsyncPostgresSaver` for async.
  - Optional encryption: `serde=EncryptedSerializer.from_pycryptodome_aes()`. On LangSmith it is automatic when `LANGGRAPH_AES_KEY` is set.
  - The default serializer is `JsonPlusSerializer` (ormsgpack/JSON, with `pickle_fallback=True` available).
  - Keep `thread_id` under 255 characters. In-memory savers lose checkpoints on restart.
  - Sources: [Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence); [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- `StateSnapshot` (from `graph.get_state(config)`) has these fields: `values`, `next` (empty means done), `config` (`thread_id`, `checkpoint_ns`, `checkpoint_id`), `metadata` (`source` = "input"/"loop"/"update", `writes`, `step`), `created_at`, `parent_config`, and `tasks` (`id`, `name`, `error`, `interrupts`, `state`). `get_state_history(config)` returns newest first. — [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- Node-level retry: `RetryPolicy` fields (source) are `initial_interval=0.5`, `backoff_factor=2.0`, `max_interval=128.0`, `max_attempts=3`, `jitter=True`, and `retry_on` (default `default_retry_on`: retries `ConnectionError` and HTTP 5xx). You pass it to `add_node(..., retry_policy=...)` or `@task(retry_policy=...)`. — [types.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)
- Node caching: `compile(cache=InMemoryCache())` plus `add_node(..., cache_policy=CachePolicy(ttl=3))`, where `key_func` defaults to a pickle hash. — [Graph API docs](https://docs.langchain.com/oss/python/langgraph/graph-api)
- Functional API — [Functional API docs](https://docs.langchain.com/oss/python/langgraph/functional-api):
  - `@entrypoint(checkpointer=...)` turns a function with a single positional input into a `Pregel` object. Injectable parameters are `previous`, `store`, `writer` and `config`.
  - `@task` calls return futures (`.result()` / `await`). Task results are checkpointed, and on resume "the task result will be loaded from the checkpoint instead of being recomputed".
  - Replay starts at the top of the entrypoint.
  - Resume after an error: "run the `entrypoint` with a `None` and the same **thread id**".
  - `entrypoint.final(value=..., save=...)` separates the return value from the saved `previous`.
  - Determinism: put non-deterministic work (time, random, API calls) and side effects inside tasks. A task that started but didn't finish may re-run, so use idempotency keys.

### Inferences
- For a browser bot, where a look-and-act step has real-world side effects, use `durability="sync"` (or at least `"async"`). Put each browser action in its own node or `@task` so a crash replays at most one action. Make actions idempotent where possible, e.g. check page state before re-clicking.
- A crash mid-run is recoverable without the chunk/self-message mechanism. A supervisor can find threads whose latest `StateSnapshot.next` is non-empty and call `graph.invoke(None, {"configurable": {"thread_id": ...}})`.

### Gaps
- The docs pages fetched did not explicitly state the default durability. It comes from source ("async").

## 3. Long-running/background runs (Agent Server / LangSmith Deployment), double-texting, cron, timeouts

### Takeaway
The hosted Agent Server (formerly LangGraph Platform/Server, now under "LangSmith Deployment") runs graphs as **background runs** on threads with a task queue. A `multitask_strategy` of `reject` / `enqueue` (default) / `interrupt` / `rollback` decides what happens when a new message arrives mid-run. Cron jobs, delayed runs (`after_seconds`), webhooks and cancel-with-interrupt-or-rollback are also built in. Double-texting is **not** available in open-source LangGraph. You would have to rebuild it yourself.

### Cited Findings
- Double-texting strategies — [Double-texting docs](https://docs.langchain.com/langsmith/double-texting):
  - **Enqueue (default)**: "the default double texting (multi-tasking) strategy". The current run finishes, then queued inputs run in sequence.
  - **Reject**: refuse new runs while one is in progress.
  - **Interrupt**: halt the current run, keep progress so far, insert the new input and continue from that state. The graph must handle edge cases such as a tool call started but not completed.
  - **Rollback**: halt the run and revert all progress, including the original run input, then run the new input fresh.
  - "Double texting is a LangSmith Deployment feature and isn't available in the open-source LangGraph framework."
- SDK `runs.create(thread_id, assistant_id, ...)` parameters (source, langgraph-sdk 0.4.6): `input`, `command`, `stream_mode`, `stream_resumable`, `metadata`, `config`, `context`, `checkpoint`/`checkpoint_id`, `durability`, `interrupt_before`/`interrupt_after`, `webhook`, `multitask_strategy: Literal["reject","interrupt","rollback","enqueue"]`, `if_not_exists: Literal["create","reject"]`, `after_seconds` (delayed start), and `on_completion: Literal["delete","keep"]` (for stateless runs). `runs.cancel(..., wait=False, action: Literal["interrupt","rollback"]="interrupt")`. `DisconnectMode = Literal["cancel","continue"]`. — [langgraph-sdk schema.py](https://github.com/langchain-ai/langgraph/blob/main/libs/sdk-py/langgraph_sdk/schema.py); [SDK runs.create reference](https://reference.langchain.com/python/langgraph-sdk/_sync/runs/SyncRunsClient/create)
- Cron: `client.crons.create(...)` (new thread per execution) and `crons.create_for_thread(...)`. Options include an IANA timezone, `end_time` (without it, the job runs indefinitely) and a multitask strategy limited to 'reject'/'interrupt'/'rollback'/'enqueue'. With `keep`, each execution creates a thread the client must clean up. — [Cron create reference](https://reference.langchain.com/python/langgraph-sdk/_sync/cron/SyncCronClient/create); [create-cron API](https://docs.langchain.com/langsmith/agent-server-api/crons/create-cron)
- Timeouts: a community forum thread points to the `BG_JOB_TIMEOUT_SECS` env var for background run duration. It notes that Cloud's 1-hour API request/stream timeout is separate from the run's own lifetime; clients reconnect to the stream. — [LangChain forum](https://forum.langchain.com/t/langsmith-deployment-max-execution-time/2112) (community, unverified against env-var docs)

### Inferences
- The bot's "posts itself a message so the dispatcher starts a fresh run" is a home-grown version of enqueue. If user messages can arrive mid-task, the platform's `interrupt` semantics (keep progress, inject the message, continue) is the model to copy. You also need to handle dangling tool calls, which Deep Agents' `PatchToolCallsMiddleware` exists to fix (section 8).
- `after_seconds`-style delayed runs and cron map well onto "wake later and continue" behavior for long browser tasks.

### Gaps
- I did not verify the exact default and maximum of `BG_JOB_TIMEOUT_SECS` from the official env-var page.

## 4. Human-in-the-loop: interrupt(), breakpoints, approve/edit, time travel

### Takeaway
`interrupt(payload)` pauses a node, persists state and surfaces the payload to the caller (`__interrupt__`). `Command(resume=value)` continues on the same thread, with the node re-executing from its start. Static `interrupt_before/after` exist for debugging. `HumanInTheLoopMiddleware` packages approve/edit/reject for tool calls. Checkpoint history allows replay and fork ("time travel").

### Cited Findings
- `interrupt()` requires a checkpointer plus a `thread_id`, and the payload must be JSON-serializable. With `invoke()` the payload appears in `result["__interrupt__"]`. With the new `graph.stream_events(..., version="v3")`, it appears on `stream.interrupts` (and `stream.interrupted`). Resume with `Command(resume=...)` and the same thread, and don't use `Command(update=...)` to continue. — [Interrupts docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- Multiple parallel interrupts: resume with `Command(resume={interrupt.id: value, ...})`. — [Interrupts docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- Rules of interrupts — [Interrupts docs](https://docs.langchain.com/oss/python/langgraph/interrupts):
  - The node restarts on resume.
  - Don't wrap `interrupt()` in a bare try/except, because it works by raising.
  - Resume matching is index-based, so keep interrupt order consistent.
  - Avoid `while True` re-prompt loops.
  - Code before `interrupt()` must be idempotent.
- Static breakpoints: `interrupt_before`/`interrupt_after` at compile or run time. Resume with `graph.invoke(None, config)`. The docs say these are not recommended for HITL; use `interrupt()` instead. — [Interrupts docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- `HumanInTheLoopMiddleware(interrupt_on={tool_name: {"allowed_decisions": ["approve","edit","reject"]} | False})` requires a checkpointer. — [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- Time travel — [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers):
  - Invoking with a prior `checkpoint_id` replays nodes after it, and interrupts are re-triggered.
  - `update_state(config, values, as_node=...)` creates a new forked checkpoint (reducers apply).
  - `get_state_history` lists the checkpoints.

### Inferences
- A browser bot hitting a sign-in or CAPTCHA wall can `interrupt({"need": "login", "url": ...})` and wait indefinitely at zero cost. A later `Command(resume=...)` continues the same thread, which beats ending the chunk.

### Gaps
- None material.

## 5. Context management for long trajectories and run-level limits (LangChain v1 middleware)

### Takeaway
LangChain v1 (`create_agent`) replaced `pre_model_hook`/`post_model_hook` with composable middleware. The ones relevant to long runs are:
- `SummarizationMiddleware`: token, fraction or message-count triggers.
- `ContextEditingMiddleware`: clears old tool outputs.
- `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`: thread- and run-scoped caps with `end`/`error`/`continue` behavior.
- `ModelRetryMiddleware` / `ToolRetryMiddleware` / `ModelFallbackMiddleware` / `ToolErrorMiddleware`.

Deep Agents adds summarization that offloads evicted history to a file, and evicts large tool results to the filesystem.

### Cited Findings
All built-in middleware below are from the [Built-in middleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in) unless noted; defaults are verified against langchain 1.4.4 source where marked.
- `SummarizationMiddleware(model, trigger=None, keep=("messages", 20), token_counter=<char-based>, summary_prompt=..., trim_tokens_to_summarize=4000)`. With `trigger=None` it never fires automatically. Trigger and keep accept `("fraction", 0.5)`, `("tokens", 3000)` or `("messages", 50)` (source: `ContextFraction`/`ContextTokens`/`ContextMessages`). `summary_prefix`, `max_tokens_before_summary` and `messages_to_keep` are deprecated.
- `ContextEditingMiddleware(edits=[ClearToolUsesEdit()], token_count_method="approximate")`. `ClearToolUsesEdit` defaults: `trigger=100000` tokens, `clear_at_least=0`, `keep=3`, `clear_tool_inputs=False`, `exclude_tools=()`, `placeholder="[cleared]"`.
- `ModelCallLimitMiddleware(thread_limit=None, run_limit=None, exit_behavior="end"|"error")`. Thread limits need a checkpointer. In source:
  - The state keys are `thread_model_call_count` (persisted) and `run_model_call_count` (`UntrackedValue`, reset per run).
  - On `"end"` it returns `{"jump_to": "end", "messages": [AIMessage(limit_message)]}`.
  - On `"error"` it raises `ModelCallLimitExceededError`.
  - Source: [model_call_limit.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/model_call_limit.py)
- `ToolCallLimitMiddleware(tool_name=None, thread_limit=None, run_limit=None, exit_behavior="continue")`. Per the source docstring: "`'continue'`: Block exceeded tools, let execution continue (default) / `'error'`: Raise an exception / `'end'`: Stop immediately with a `ToolMessage` for each exceeded tool". At least one limit is required. — [tool_call_limit.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/tool_call_limit.py)
- `ModelRetryMiddleware` / `ToolRetryMiddleware` defaults: `max_retries=2`, `retry_on=default_retry_on`, `on_failure="continue"` (or `"error"`/callable), `backoff_factor=2.0`, `initial_delay=1.0`, `max_delay=60.0`, `jitter=True`. `ModelFallbackMiddleware(first_model, *additional_models)`. `ToolErrorMiddleware(on_error=..., aon_error=..., tools=None)` converts tool exceptions into model-visible messages (langchain ≥1.3.14).
- Others: `TodoListMiddleware` (`write_todos` tool), `LLMToolSelectorMiddleware(max_tools, always_include)`, `ProviderToolSearchMiddleware` (Anthropic/OpenAI), `PIIMiddleware`, `ShellToolMiddleware`, `FilesystemFileSearchMiddleware`, `LLMToolEmulator`, plus Anthropic prompt-caching middleware.
- `pre_model_hook` exists only on the deprecated `create_react_agent`. It can return `llm_input_messages` to trim or summarize the model input without rewriting state. — [chat_agent_executor.py](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py) (source)
- LangMem short-term: `SummarizationNode(model, max_tokens, max_tokens_before_summary=None, max_summary_tokens=256, output_messages_key="summarized_messages")`, `summarize_messages(...)` and a `RunningSummary` state object. — [langmem short_term/summarization.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/short_term/summarization.py) (source, langmem 0.0.30)
- Deep Agents summarization (`deepagents.middleware.summarization`) — [deepagents summarization.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) (source):
  - `SummarizationMiddleware(model, backend, trigger=("fraction",0.85), keep=("fraction",0.10))` (docstring example).
  - Evicted messages are appended to `/conversation_history/{session_id}.md` on the backend, so they remain recoverable.
  - `create_summarization_middleware` auto-picks fraction thresholds from the model profile's `max_input_tokens`.
  - `SummarizationToolMiddleware` adds a `compact_conversation` tool so the agent can compact on demand.
- Deep Agents `FilesystemMiddleware(tool_token_limit_before_evict=20000)` writes oversized tool results to `/large_tool_results/` and leaves a pointer, at about 4 chars per token. — [deepagents filesystem.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py) (source)

### Inferences
- The bot's LLM-call and tool-call ceilings correspond directly to `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`. The useful distinction is `thread_limit` (across all chunks/resumes of the task, persisted via checkpointer) versus `run_limit` (per invocation). The "5 chunks" cap is better expressed as a thread-level model-call or cost budget than as a chunk count.
- For browser screenshots and DOM snapshots, `ContextEditingMiddleware`-style clearing of old tool outputs (keep the last N=3 observations) plus offloading large snapshots to files gets most of the context savings without losing the action log.

### Gaps
- I found no built-in wall-clock or cost-ceiling middleware in langchain 1.4.4. Those must be custom (`before_model` hook checking elapsed time or tokens and returning `jump_to: "end"`).

## 6. Recovering from malformed structured output / tool-call errors

### Takeaway
In v1 the main path is `create_agent(response_format=ToolStrategy(Schema, handle_errors=...))`. Validation errors are fed back to the model as a ToolMessage and it retries. `ToolNode` likewise returns argument-validation errors to the model ("Please fix the error and try again."). The old `OutputFixingParser` / `RetryWithErrorOutputParser` survive only in `langchain-classic`. Model-level resilience comes from `ModelRetryMiddleware` / `ModelFallbackMiddleware` or Runnable `.with_retry()` / `.with_fallbacks()`.

### Cited Findings
- `ToolStrategy.handle_errors: bool | str | type[Exception] | tuple[...] | Callable[[Exception], str]`. Per the docstring: "`True`: Catch all errors with default error template; `str`: … custom message; … `False`: No retry, let exceptions propagate." Warning: when the schema is a raw JSON-schema `dict`, arguments are "returned as-is without validation", so `handle_errors` is effectively inert. Use Pydantic, dataclass or TypedDict for validation and retries. — [langchain agents/structured_output.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/structured_output.py) (source, langchain 1.4.4)
- `ToolNode(handle_tool_errors=...)` defaults to `_default_handle_tool_errors`, which converts `ToolInvocationError` (pydantic arg validation) into the message `"Error invoking tool '{tool_name}' with kwargs {tool_kwargs} with error:\n {error}\n Please fix the error and try again."` and re-raises other errors. — [prebuilt tool_node.py](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py) (source)
- `OutputFixingParser` (`max_retries: int = 1`) and `RetryWithErrorOutputParser` now live in `langchain_classic.output_parsers.fix` / `.retry` (langchain-classic 1.0.8), i.e. the legacy package. — [langchain-classic on PyPI](https://pypi.org/project/langchain-classic/) (source)
- `ModelRetryMiddleware`, `ModelFallbackMiddleware` and `ToolErrorMiddleware`: see section 5. — [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

### Inferences
- For the bot's look-and-act LLM output, model the action as a tool call with a Pydantic schema and let validation errors return to the model as a tool message. That works better than one-shot `with_structured_output` plus a fixing parser, and gives retries for free within the same pass. Cap these retries with `ToolCallLimitMiddleware` or a counter.

### Gaps
- I did not fetch the current docs on `with_structured_output(method=..., include_raw=True)` behavior for v1 chat models.

## 7. Memory: short-term (thread) vs long-term Store; LangMem procedural / semantic / episodic

### Takeaway
Short-term memory is the checkpointed thread state. Long-term memory is a `BaseStore` (namespaced key-value with optional semantic search) passed at `compile(store=...)`. LangMem builds on this: memory managers, memory tools, a background `ReflectionExecutor`, and prompt optimizers (`gradient` / `metaprompt` / `prompt_memory`) that learn updated instructions from scored trajectories, i.e. procedural memory.

### Cited Findings
- Checkpointers "persist a thread's graph state as checkpoints". Stores "persist application-defined data outside the graph state". Compile with `builder.compile(checkpointer=..., store=...)`. Subgraphs get their own checkpoint namespace, and you use the Store for cross-graph data. — [Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- LangMem memory types — [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/):
  - Semantic: a collection or a profile.
  - Episodic: past successful interactions with observation, thoughts, action and result.
  - Procedural: core instructions evolved through feedback via prompt optimizers.
- LangMem APIs — [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/):
  - `create_memory_manager(model, instructions=..., schemas=..., enable_inserts=...)`, `create_memory_store_manager` (persists to the LangGraph store), `create_manage_memory_tool`, `create_search_memory_tool`.
  - Namespaces may template `configurable` fields, e.g. `("acme_corp", "{user_id}", "code_assistant")`.
  - Hot-path formation (agent writes during the conversation) versus background formation (after the conversation or idle time).
- `create_prompt_optimizer(model, kind="gradient"|"metaprompt"|"prompt_memory", config=...)`, invoked with `{"trajectories": [(messages, feedback), ...], "prompt": current}`. There is also a multi-prompt optimizer (`kind="single"|"multi"` in `prompts/_layers.py`). `ReflectionExecutor(...).submit(payload, config, after_seconds=0)` defers memory processing to the background. — [langmem prompts/optimization.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/prompts/optimization.py), [langmem reflection.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/reflection.py) (source, langmem 0.0.30)

### Inferences
- The bot's per-task "plan and notes" are short-term memory (thread state). Lessons about a site, e.g. "login button is in the hamburger menu on X", belong in a Store namespace like `("sites", domain)` and should be searched at task start. Procedural lessons from failed trajectories could feed `create_prompt_optimizer` offline.
- langmem is still 0.0.x, so treat its API as unstable.

### Gaps
- I did not verify current maintenance status or deprecation of langmem relative to Deep Agents' `MemoryMiddleware` (AGENTS.md-style memory files).

## 8. Loop/stall detection and Deep Agents (planning, sub-agents, filesystem offloading)

### Takeaway
Neither LangGraph nor LangChain 1.4.4 ships a generic loop or stall detector. The documented LangChain approach is harness middleware:
- `LoopDetectionMiddleware`, from the Feb 2026 harness-engineering post: counts repeated edits to the same file and injects a "reconsider your approach" nudge.
- Pre-completion verification.
- Time-budget warnings.
- `RubricMiddleware`: grader-driven continuation, beta in deepagents.

Deep Agents' default stack is filesystem, sub-agents (`task` tool), summarization with offload, `PatchToolCallsMiddleware`, skills, memory and HITL. As of 0.7, the todo planner is no longer in the default stack.

### Cited Findings
- A grep of the langchain 1.4.4, langgraph 1.2.14 and deepagents 0.7.23 sources for loop, stall or repeated-call detection found no such middleware. The only "infinite loops" mention is the recursion-limit docstring in `langgraph/errors.py` (source).
- LangChain blog "Improving Deep Agents with harness engineering" (Feb 17, 2026, Vivek Trivedy) — [LangChain blog](https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering):
  - **LoopDetectionMiddleware**: tracks per-file edit counts via tool-call hooks and, after N edits, adds context like "consider reconsidering your approach". N is unspecified. Traces showed "10+" variations on the same broken approach, and the model can still continue.
  - **PreCompletionChecklistMiddleware**: intercepts exit and forces a verification pass, likened to a "Ralph Wiggum Loop".
  - Time-budget warnings nudge the agent toward finishing and verifying.
  - `LocalContextMiddleware` maps the environment at start.
  - Results: Terminal Bench 2.0 went from 52.8% to 66.5% with gpt-5.2-codex, harness-only changes. A "reasoning sandwich" (xhigh plan / high implement / xhigh verify) was used; xhigh-only scored 53.9% due to timeouts versus 63.6% at high.
- `create_deep_agent(model, tools, system_prompt, middleware, subagents, skills, memory, permissions, backend, interrupt_on, response_format, checkpointer, store, ...)`. Its default main-agent stack (source, deepagents 0.7.23 `graph.py`):
  - `FilesystemMiddleware` (ls/read_file/write_file/edit_file + large-result eviction)
  - `SubAgentMiddleware` (`task` tool, with a default general-purpose subagent)
  - `create_summarization_middleware(model, backend)`
  - `PatchToolCallsMiddleware` (repairs dangling tool calls)
  - optional `AsyncSubAgentMiddleware`
  - `SkillsMiddleware`
  - Anthropic prompt caching
  - `MemoryMiddleware`
  - `HumanInTheLoopMiddleware`
  - `UnsupportedContentMiddleware`
  - There is no `TodoListMiddleware` in `graph.py`.
  - Source: [deepagents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
- A third-party summary says v0.7 removed the todo planner from the default harness because LangChain's evals found it added cost and latency without improving performance. — [TrueFoundry blog](https://www.truefoundry.com/blog/what-are-langgraph-deep-agents-the-harness-explained) (secondary; consistent with the source above)
- `RubricMiddleware` (beta, deepagents ≥0.6.5): "Each time the agent would otherwise finish … invokes a separate grader sub-agent against the transcript. If the grader returns `needs_revision`, its feedback is injected as a `HumanMessage` and the agent loop resumes … until … `satisfied` or `failed`, or `max_iterations` is reached." — [deepagents rubric.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/rubric.py) (source); [Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

### Inferences
- For a browser bot, the analog of per-file edit counting is counting repeated (URL, action, target) tuples or unchanged page hashes across passes. When a threshold is crossed, inject a "you appear stuck; change approach / ask for help / give up" message rather than hard-stopping. This is cheap to implement as a `before_model` / `wrap_tool_call` middleware or a graph node.
- Sub-agents with isolated context (the `task` tool) are LangChain's answer to long trajectories. The parent keeps a short plan and log, and each sub-task runs in a fresh context and returns a summary. This is the same idea as the bot's chunking, but structured.

### Gaps
- The `LoopDetectionMiddleware` from the blog does not appear to ship in deepagents 0.7.23 or langchain 1.4.4; the source grep found nothing. Its exact thresholds and code are unpublished in the sources I found.
- I found no official LangGraph guidance on stall detection based on wall-clock time or lack of progress.
