# OpenAI: how its agent products and SDKs keep long-running agent tasks going

Scope: ChatGPT agent (formerly Operator / CUA), the OpenAI Agents SDK (Python and JS), the Responses API (background mode, compaction, conversation state, computer tool), and Codex (CLI, app-server, cloud). Researched 2026-10-09. Primary sources were used where reachable. openai.com and help.openai.com returned HTTP 403 to the fetcher, so ChatGPT agent product facts come from the system card hub (deploymentsafety.openai.com), the system card PDF, and search snippets. Each such case is flagged below. Model names quoted from current docs (e.g. `gpt-6-astra`, `gpt-6.1-sol`, `gpt-5.6-sol`) are given as the pages show them.

Main conclusion for the in-house bot: none of OpenAI's current long-horizon products uses a small fixed step count as the main stop condition. The Agents SDK does default to 10 turns, but the limit is an overridable safety net (`max_turns=None` turns it off), and an error handler can soften it. The long-running products (Codex `/goal`, ChatGPT agent) instead rely on:
- token or time budgets
- context compaction
- evidence-based completion audits
- explicit "blocked" and "no-progress" classification
- human pause and resume

---

## 1. Step/turn limits, and the time, cost or token budgets used instead

### Takeaway
The Agents SDK's default `max_turns` is 10 in both Python and JS. Going over it raises `MaxTurnsExceeded`. You can set `max_turns=None` to remove it, or register a `"max_turns"` error handler that returns a graceful final output instead. Codex has no turn or step limit in its config. Its long-running mode (`/goal`) is bounded by a token budget with a `budget_limited` wrap-up state, plus a blocked/no-progress audit. For the raw computer-use tool, OpenAI tells developers to "set step, time, or cost limits" themselves.

### Cited Findings
- **Agents SDK default (Python).** `src/agents/run_config.py` defines `DEFAULT_MAX_TURNS = 10`. The run loop raises `MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")`. — [openai-agents-python run_config.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/run_config.py), [run_loop.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/run_internal/run_loop.py)
- **Agents SDK default (JS).** `packages/agents-core/src/runner/constants.ts` has `export const DEFAULT_MAX_TURNS = 10;`. The option is `maxTurns`, and it is stored on the run state as `state._maxTurns`. — [openai-agents-js constants.ts](https://github.com/openai/openai-agents-js/blob/main/packages/agents-core/src/runner/constants.ts)
- **Signature.** `Runner.run(..., max_turns: int | None = DEFAULT_MAX_TURNS, ..., error_handlers: RunErrorHandlers[TContext] | None = None)`. Passing `None` disables the limit. A "turn" is one LLM call. — [Agents SDK Runner reference](https://openai.github.io/openai-agents-python/ref/run/), [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Error handlers.** `error_handlers` is a dict keyed by `"max_turns"`, `"model_refusal"` and `"invalid_final_output"`.
  - The `"max_turns"` handler returns a `RunErrorHandlerResult` whose `final_output` goes back to the caller in place of the exception.
  - `RunErrorHandlerResult.include_in_history` defaults to `True`, which means the fallback is appended to history and saved to the session.
  - Source: [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Cancel and resume.** A run stopped with `cancel(mode="after_turn")` can be resumed by passing its `RunState` back to `Runner.run`, because RunState is "passed as input to resume a paused run or a run stopped with `cancel(mode="after_turn")`". — [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Per-attempt model timeout.** `ModelSettings.timeout` bounds each model-call attempt and raises `ModelTimeoutError`. The docs say it "does not limit the full agent run, function-tool execution, or retry backoff". — [Agents SDK Models docs](https://github.com/openai/openai-agents-python/blob/main/docs/models/index.md)
- **Tool timeouts.** The default is `timeout_behavior="error_as_result"`, which sends a model-visible message such as "Tool 'slow_lookup' timed out after 2 seconds." so the model can recover. `"raise_exception"` instead raises `ToolTimeoutError` and fails the run. — [Agents SDK Tools docs](https://github.com/openai/openai-agents-python/blob/main/docs/tools.md)
- **Computer-use guide (Responses API).**
  - The code-execution example caps its loop at 20 responses and raises an error if it reaches that cap.
  - The computer-tool section sets no number. Instead it advises: "Set step, time, or cost limits, support cancellation, and check the actual outcome."
  - Source: [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
- **Codex config has no step limit.** The config reference has no max-turns or step-limit key. Related keys:
  - `agents.max_concurrent_threads_per_session` caps the number of spawned sub-agent threads.
  - `features.rollout_budget.*` provides token-based budget tracking. It is off by default, and `limit_tokens` is required when it is enabled.
  - `features.goals` provides "Persisted goals and automatic continuation". It is stable and on by default.
  - Source: [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference) (redirected from developers.openai.com/codex/config-reference)
- **Codex Goals budget.**
  - A Goal records the objective, lifecycle state, budget and progress accounting.
  - Automatic continuation happens only when the thread is idle, the Goal is active and within budget, and no user input is queued.
  - When the budget is reached, Codex should "stop substantive work, summarize progress and blockers, and name the next useful step". "Reaching a budget limit is not the same as completing the objective."
  - Source: [Using Goals in Codex (OpenAI Cookbook)](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
- **Codex `budget_limit.md` template.** The prompt injected when the budget runs out includes "Time spent pursuing goal: {{ time_used_seconds }} seconds", tokens used and the token budget. It also says: "The system has marked the goal as budget_limited, so do not start new substantive work… Wrap up this turn soon: summarize useful progress, identify remaining work or blockers, and leave the user with a clear next step." — [codex-rs/ext/goal/templates/goals/budget_limit.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/budget_limit.md)
- **Codex goal stop reasons in the runtime.** `ActiveGoalStopReason` maps to goal statuses as follows:
  - `TurnError` → `Blocked`
  - `UsageLimit` → `UsageLimited`
  - `EmptyResponse` → `Blocked`
  - `ExecutionUnavailable` → `Blocked`

  There is also a `BudgetLimited` status. — [codex-rs/ext/goal/src/runtime.rs](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/src/runtime.rs)
- **Codex long-horizon claim.** GPT-5.1-Codex-Max "automatically compacts its session when it approaches its context window limit", and OpenAI says that in internal testing it worked on tasks for more than 24 hours. This is quoted via secondary coverage, because OpenAI's own page was not reachable. — [Neowin](https://www.neowin.net/news/openai-announces-gpt51-codex-max-a-new-coding-model-built-for-long-running-tasks/), [gend.co](https://www.gend.co/blog/gpt-5-1-codex-max-compaction)
- **Older ChatGPT agent usage limits (July 2025, likely superseded).** A secondhand forum report lists monthly agent message caps: Pro 400, Plus 40, Team 30 credits. The same report says runs can last about 40 minutes. Treat both as unverified. — [Habr comments](https://habr.com/en/news/930274/comments)

### Inferences
- OpenAI's own SDK keeps a low turn cap (10) as a guard against runaway loops in short, tool-using chat agents. Its long-horizon product (Codex Goals) replaces turn counting with token and time budgets plus a semantic stop condition: complete, blocked or budget-limited.
- Hitting the budget is designed to end gracefully: a wrap-up turn that summarizes progress and the next step, never a hard abort. That maps directly onto replacing the bot's "24 steps × 5 chunks" hard stop with a budget plus a forced wrap-up/handoff turn.
- For the raw computer-use tool, OpenAI leaves step, time and cost limits to the integrator, so there is no OpenAI-endorsed numeric default for browser-agent steps.

### Gaps
- Current ChatGPT agent per-task time limit and current usage caps: help.openai.com returned 403, and no primary source was found.
- Codex cloud per-task wall-clock limit: no official number was found. A "7 hours" figure appears only on third-party blogs ([codex.danielvaughan.com](https://codex.danielvaughan.com/2026/04/16/codex-cli-context-compaction-tuning-long-sessions/)) and is unverified.
- Default Codex goal token budget: neither the cookbook nor the templates give one.

---

## 2. Keeping context bounded over long trajectories

### Takeaway
OpenAI uses compaction, not plain truncation. The Responses API offers two forms:
- **Server-side automatic compaction.** Set `context_management: [{type: "compaction", compact_threshold: N}]`. When the rendered token count crosses `N`, the server emits an opaque encrypted compaction item, prunes the context and keeps going within the same response.
- **A stateless `/responses/compact` endpoint.**

The Agents SDK wraps this as `OpenAIResponsesCompactionSession`. Codex auto-compacts at `model_auto_compact_token_limit`, using a "handoff summary" prompt, and caps tool outputs stored in history with `tool_output_token_limit`.

### Cited Findings
- **Server-side compaction.** Enable it with `context_management`, e.g. `{ type: "compaction", compact_threshold: 200000 }`.
  - When the rendered tokens cross the threshold, the server compacts. The stream includes an encrypted compaction item, and the server "prunes context before continuing inference". No separate call is needed.
  - The docs give no default threshold. The examples use 200,000.
  - Source: [Compaction guide](https://developers.openai.com/api/docs/guides/compaction)
- **Encrypted compaction items.** These carry "key prior state and reasoning using fewer tokens". They are opaque, and the caller should pass them back unchanged. — [Compaction guide](https://developers.openai.com/api/docs/guides/compaction)
- **Chaining after compaction.** There are two patterns:
  - Stateless input-array chaining: append all output items, including compaction items. You may drop items that come before the most recent compaction item.
  - `previous_response_id` chaining. With this pattern the docs say: "do not manually prune."
  - Source: [Compaction guide](https://developers.openai.com/api/docs/guides/compaction)
- **`/responses/compact` endpoint.**
  - It is explicit and stateless: you send the full window (which must still fit in the model's context), and it returns a new compacted window with an encrypted compaction item plus retained items.
  - The docs say: "Do not prune this output". It is the canonical next context.
  - It is "fully stateless and ZDR-friendly". Server-side compaction is also ZDR-friendly with `store=false`.
  - Source: [Compaction guide](https://developers.openai.com/api/docs/guides/compaction)
- **Agents SDK compaction session.**
  - `OpenAIResponsesCompactionSession` wraps another session (e.g. `SQLiteSession`) and auto-compacts after each turn via `should_trigger_compaction`.
  - Modes: `compaction_mode="previous_response_id"` | `"input"` | `"auto"` (default). With `store=False`, `"auto"` falls back to input-based compaction.
  - Auto-compaction blocks the end of the run or stream until it finishes. For low latency, disable it and call `run_compaction()` between turns or during idle time.
  - The wrapper restores the previous history if a replacement fails, and skips stale compactions.
  - Source: [Agents SDK Sessions docs](https://github.com/openai/openai-agents-python/blob/main/docs/sessions/index.md)
- **Agents SDK conversation-state strategies.** There are four:
  - client-managed: `result.to_input_list()` or a `session`
  - OpenAI-managed: `conversation_id` (Conversations API) or `previous_response_id`
  - `auto_previous_response_id=True` chains automatically.

  `conversation_id` and `previous_response_id` are mutually exclusive, and sessions cannot be combined with either. `reasoning_item_id_policy` defaults to `"preserve"` and can be set to `"omit"`. — [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Browser state lives outside the model.** On `previous_response_id`, the computer-use guide says: "Continuing a response does not restore a browser session, login state, or runtime variables." Preserve tool calls and their outputs, and keep the environment running in your application. — [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
- **Screenshot handling.** Return a fresh screenshot when the UI state is unknown and after short groups of actions. Use `detail: "original"`, and remap coordinates if you downscale. — [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
- **Codex auto-compaction config.**
  - `model_auto_compact_token_limit` is the trigger threshold. Unset means model defaults apply.
  - `model_auto_compact_token_limit_scope` is `total` (default) or `body_after_prefix`, which counts only growth after the carried compaction prefix.
  - Related keys: `compact_prompt` and `experimental_compact_prompt_file` (prompt overrides) and `model_context_window`.
  - `tool_output_token_limit` is the per-tool-output token budget in history. `mcp_servers.<id>.tools.<tool>.output_token_limit` is the per-MCP-tool override.
  - `skills.max_context_tokens` defaults to 2% of the context window, capped at 10,000.
  - Source: [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Codex compaction prompt (verbatim).** "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: Current progress and key decisions made; Important context, constraints, or user preferences; What remains to be done (clear next steps); Any critical data, examples, or references needed to continue." — [codex-rs/prompts/templates/compact/prompt.md](https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/compact/prompt.md)
- **Codex summary prefix.** After compaction, the new context begins: "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work…" — [summary_prefix.md](https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/compact/summary_prefix.md)
- **Codex compaction internals.**
  - `codex-rs/core/src/compact.rs` sets `COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000`. This appears to cap how much recent user-message text is retained verbatim across a compaction (inferred from the code name and usage).
  - The repo also has `compact_remote_v2*.rs` (remote/server compaction, including an image budget), `compact_model_fallback.rs`, and `state/auto_compact_window.rs`. The last of these tracks numbered compaction "windows" and a `token_budget_reminder_delivered` flag.
  - Source: [codex compact.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs), [auto_compact_window.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/state/auto_compact_window.rs)
- **Goal continuation tells the model to re-ground after compaction.** The template says: "Use the current worktree and external state as authoritative. Previous conversation context can help locate relevant work, but inspect the current state before relying on it." — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Known compaction failure modes (third-party, unverified).**
  - A GitHub issue reported that GPT-5-Codex "loses the plot" after auto-compaction, forgetting edits and that it was mid-task.
  - A May 2026 blog reports that auto-compaction fired too late under GPT-5.5 because it was based on the advertised window size.
  - Sources: [codex issue via upd.dev](https://upd.dev/openai/codex/issues/5957), [danielvaughan blog](https://codex.danielvaughan.com/2026/05/10/codex-cli-context-compaction-gpt55-failures-resilient-long-sessions/)

### Inferences
- The pattern to copy for a browser bot has three parts:
  1. Compact into a structured handoff summary (progress, decisions, constraints, remaining steps, critical data) when a token threshold is crossed, not every N steps.
  2. Keep large tool outputs (DOM, screenshots) under a per-output token cap.
  3. After compaction, have the agent re-observe the live environment rather than trust the summary.
- Encrypted compaction items are only usable with OpenAI models. An in-house LangGraph bot on other providers would reproduce the Codex-style plaintext handoff summary.

### Gaps
- Encrypted reasoning items (`include: ["reasoning.encrypted_content"]` with `store=false`): not re-verified in this session, so they are not cited here.
- The Responses API `truncation: "auto"` parameter, which older computer-use-preview/Operator-era docs required: the current computer-use guide does not mention it. It may be superseded by compaction, but this is unverified.
- Default Codex `model_auto_compact_token_limit` per model (for example, a percentage of the window): the docs say only "model defaults".

---

## 3. Waiting on slow external jobs: background mode, polling, webhooks

### Takeaway
The Responses API has `background: true`. You poll `GET /v1/responses/{id}` while the status is `queued` or `in_progress`, and cancel with `POST /v1/responses/{id}/cancel`. With `stream: true` you can resume a dropped stream from a `sequence_number` cursor. Agents SDK tools time out softly by default, and durable-execution integrations (Temporal, Dapr, Restate, DBOS) handle very long runs. Codex Goals separate a "verified wait" (polling a confirmed-live job) from no-progress, and say an observation timeout must not cause a restart.

### Cited Findings
- **Background mode basics.**
  - Set `background: true` on `POST /v1/responses`, then poll `GET /v1/responses/{id}` while `status` is `queued` or `in_progress`. The examples wait about 2 seconds between polls.
  - Cancel with `POST /v1/responses/{id}/cancel`. Cancellation is idempotent.
  - Source: [Background mode guide](https://developers.openai.com/api/docs/guides/background)
- **Resumable streaming.**
  - Create the response with `background: true` and `stream: true`, and keep each event's `sequence_number` as a cursor.
  - Resume with `GET /v1/responses/{id}?stream=true&starting_after={cursor}`. The response keeps running while the client is disconnected.
  - Time to first token is higher for background responses than for synchronous ones.
  - Source: [Background mode guide](https://developers.openai.com/api/docs/guides/background)
- **Retention.**
  - With ZDR, background runs with `store=false`, and data is kept on disk for about 10 minutes to allow polling.
  - With Modified Abuse Monitoring, a background response is deleted after about 10 minutes unless `store=true` is set explicitly.
  - Source: [Background mode guide](https://developers.openai.com/api/docs/guides/background)
- **Durable execution in the Agents SDK.** Listed integrations:
  - Temporal ("durable, long-running workflows, including human-in-the-loop tasks")
  - Dapr
  - Restate
  - DBOS ("Preserves progress across failures and restarts"; needs only SQLite or Postgres)

  Source: [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Agents SDK tool timeouts.** The default `timeout_behavior="error_as_result"` returns a timeout message to the model so it can recover, rather than failing the run. — [Agents SDK Tools docs](https://github.com/openai/openai-agents-python/blob/main/docs/tools.md)
- **Codex background terminals.** `background_terminal_max_timeout` (default 300000 ms) is the maximum poll window for empty `write_stdin` polls of background terminals. `features.unified_exec` is a PTY-backed exec tool for long-lived processes. — [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Codex "verified wait" rule (verbatim from continuation.md).** "A verified wait polls a specific process, session, job, or tool handle confirmed live now. Conversation, intent, prior output, or a lock or state file alone is insufficient. Treat work as stopped only when authoritative state says it is terminal or its handle is missing. An observation timeout or transient polling failure is not terminal: re-poll the same handle or inspect other authoritative state; never restart solely because observation expired." — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)

### Inferences
- For a browser bot waiting on slow pages or jobs, two OpenAI patterns apply:
  1. Run the long task asynchronously (background plus poll or resumable stream), so a client disconnect does not kill it.
  2. Count a verified wait on a live handle as progress, not as a wasted step, so waiting does not use up a step budget or trigger stall detection.

### Gaps
- Webhooks: the background-mode guide does not mention them. OpenAI webhooks for `response.completed` and similar events were not verified in this session.

---

## 4. Human-in-the-loop: confirmations, takeover, interruption/steering, approvals

### Takeaway
ChatGPT agent:
- pauses for confirmation before consequential, state-changing actions
- turns on "Watch Mode" in sensitive logged-in contexts, which auto-pauses when the user goes idle or navigates away
- lets the user take over the browser to enter credentials
- can be interrupted and redirected mid-task

The Agents SDK uses `needs_approval` → `RunResult.interruptions` → `RunState` (serializable and durable) → `approve`/`reject` → resume. Codex has `approval_policy`, an `auto_review` reviewer, and `/goal pause|resume`.

### Cited Findings
- **ChatGPT agent confirmations.** The agent "will pause and ask the user to confirm before taking certain kinds of actions online… users can review the current state and indicate whether it should proceed." It is trained to ask before finalizing actions that change the state of the world, such as purchases or sending email.
  - Confirmation recall is 91.0%, which OpenAI calls an underestimate.
  - Critical confirmation recall: 100% for editing permissions, 99.9% for high-stakes communications, 100% for financial transactions.
  - Source: [ChatGPT Agent System Card – Deployment Safety Hub](https://deploymentsafety.openai.com/chatgpt-agent/watch-mode); [System card PDF (July 17, 2025)](https://cdn.openai.com/pdf/6bcccca6-3b64-43cb-a66e-4647073142d7/chatgpt_agent_system_card_launch.pdf)
- **Watch Mode.** When the visual browser is used in a sensitive context (logged into email or banking), Watch Mode stays on "for the rest of the trajectory", "automatically pausing execution when the user becomes inactive or navigates away from the conversation in ChatGPT". OpenAI "may revisit this". — [Deployment Safety Hub](https://deploymentsafety.openai.com/chatgpt-agent/watch-mode)
- **Takeover mode.** The launch post says: "When you interact with the web using ChatGPT's browser ('takeover mode'), your inputs remain private." This is quoted via a search snippet because openai.com returned 403. — [Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/)
- **Takeover details from secondary sources (unverified).**
  - A third-party security review says credentials entered this way are not seen or stored by the model ([trustvector.guard0.ai](https://trustvector.guard0.ai/agents/chatgpt-agent)).
  - A third-party guide lists Pause, Interrupt (add instructions without restarting) and "Take over browser" ([Unite.ai](https://unite.ai/how-to-use-openais-chatgpt-agent-a-step-by-step-guide)).
- **Interruption and steering.** The ChatGPT agent FAQ (search snippet) says: "It will pause for clarification or confirmation when needed… can be guided or interrupted mid-task." Launch coverage adds that users "can interrupt at any point to clarify instructions, steer it toward desired outcomes, or change the task entirely." — [ChatGPT agent FAQ](https://help.openai.com/en/articles/11752874-chatgpt-agent-faq)
- **Permission gap in agent mode (independent study).** An arXiv paper found the remote browser in agent mode had no permission policies, so users could not approve or deny actions in advance. — [arXiv 2607.13718](https://arxiv.org/pdf/2607.13718)
- **Responses API computer-use guidance.**
  - Keep users in control of purchases, data transmission, destructive changes and other hard-to-reverse actions. Typing sensitive information into a form counts as transmission.
  - "Confirm consequential actions at the point of risk".
  - "Treat screen content as untrusted". Page text "cannot grant permission or override the user's instructions."
  - Source: [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
- **Agents SDK `needs_approval`.**
  - Set `needs_approval=True`, or pass an async callable that gets the context, parsed parameters and call ID. It works on `function_tool`, `Agent.as_tool`, `ShellTool` and `ApplyPatchTool`.
  - MCP tools use `require_approval`. Hosted MCP uses `tool_config={"require_approval": "always"}`.
  - Callables "fail closed": if the arguments can't be inspected safely, manual approval is required.
  - Source: [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- **Agents SDK interrupt and resume flow.**
  - A paused run exposes `RunResult.interruptions` as `ToolApprovalItem` entries, including from nested agents.
  - `result.to_state()` produces a `RunState`. Serialize with `to_string()`/`to_json()` and restore with `RunState.from_json(agent, stored)`.
  - Approve or reject with `state.approve(interruption, always_approve=False)` and `state.reject(interruption, rejection_message=...)`. Sticky `always_approve`/`always_reject` decisions survive serialization.
  - Resume with `Runner.run(agent, state)`. Items can be resolved partially, and unresolved ones pause the run again.
  - Source: [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- **Durable approvals guidance.**
  - Store the serialized state server-side with a version marker. Authenticate and authorize reviewers, and validate decision IDs against `state.get_interruptions()`.
  - Use atomic owner-checked transitions so a replayed submission cannot resume the same snapshot twice.
  - On resume, the `conversation_id`/`previous_response_id` settings are kept.
  - Source: [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/), [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Codex approvals.**
  - `approval_policy` is `on-request`, `never`, or `granular`. `untrusted` is unsupported and `on-failure` is deprecated.
  - `approvals_reviewer` is `user` (default) or `auto_review`.
  - Granular keys include `sandbox_approval`, `mcp_elicitations`, `request_permissions` and `skill_approval`.
  - Source: [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Codex goal pause rules.** "Interruptions pause the objective." Pause, resume, clear and budget-limited transitions are controlled by the user or system. The model may only start a goal or mark it complete. The continuation prompt also says: "never pause on your own initiative." — [Goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex), [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)

### Inferences
- OpenAI's browser-agent safety model has three layers:
  1. A model-trained, action-scoped confirmation before irreversible actions.
  2. A context-scoped supervision mode (Watch Mode) that pauses when the human leaves.
  3. Explicit takeover for credentials.

  None of these is a step cap. Pausing is a state the task can return from: it resumes without losing progress.
- For LangGraph, `RunState` plus `interruptions` corresponds to LangGraph `interrupt()` with a checkpointer. The SDK's security guidance (authenticated reviewers, replay protection, versioned snapshots) carries over directly.

### Gaps
- Current ChatGPT agent UI details for takeover, interruption and notifications (help center 403). The system card is dated July 2025, and current behavior may differ.

---

## 5. Recovering from malformed model output

### Takeaway
The Agents SDK uses strict JSON schemas for structured tool output. It returns errors to the model rather than crashing in several cases: tool exceptions via `failure_error_function`, tool timeouts, and optionally unknown tool names via `tool_not_found_behavior="return_error_to_model"`. For a final output that fails validation, it offers an `"invalid_final_output"` fallback handler, which does not retry. Network and server retries are opt-in through `ModelRetrySettings`.

### Cited Findings
- **Unknown tool names.** `tool_not_found_behavior` defaults to raising `ModelBehaviorError`. Setting it to `"return_error_to_model"` appends a `function_call_output` and reruns the model. This applies only to failed function-name lookups. — [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Tool exceptions.** `failure_error_function` defaults to `default_tool_error_function`, which "tells the LLM an error occurred". `tool_error_formatter` (in RunConfig) customizes messages for kinds such as `"approval_rejected"` and `"tool_not_found"`. — [Tools docs](https://github.com/openai/openai-agents-python/blob/main/docs/tools.md), [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Invalid final output.** The `"invalid_final_output"` handler supplies a fallback when output fails `output_type` validation. The SDK validates the fallback against `output_type`, and "does not retry the model or replay tool side effects". Returning `None` declines recovery. — [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Strict output schemas.** Structured return annotations (Pydantic, TypedDict, dataclass) become strict object output schemas, and returned values are validated. For schema-backed calls, the default free-form failure formatter is disabled. — [Tools docs](https://github.com/openai/openai-agents-python/blob/main/docs/tools.md)
- **Runner-managed retries.** These are opt-in. "The SDK does not retry general model requests unless you set `ModelSettings(retry=...)`."
  - `ModelRetrySettings(max_retries, backoff, policy)` with policies `retry_policies.provider_suggested()`, `retry_after()`, `network_error()` and `http_status([408,409,429,500,502,503,504])`.
  - Replay-safety facts (`replay_safety`, `stateful_request`, `response_started`) mean the SDK will not replay a request once response events have arrived.
  - `conversation_locked` errors are retried automatically with backoff.
  - Source: [Models docs](https://github.com/openai/openai-agents-python/blob/main/docs/models/index.md), [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- **Codex transport retries.** Defaults: `request_max_retries` 4, `stream_max_retries` 5, `stream_idle_timeout_ms` 300000. — [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Codex empty responses.** An empty model response during a goal (`ActiveGoalStopReason::EmptyResponse`) moves the goal to `Blocked` rather than looping. — [runtime.rs](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/src/runtime.rs)

### Inferences
- OpenAI's default is to keep the loop alive by feeding errors back to the model as tool results, and to escalate (raise or block) only on structural failures. A malformed action in the browser bot should become a model-visible error observation, not a run-ending exception, and it should not count heavily against a hard step budget.

### Gaps
- Responses API Structured Outputs `strict: true` guarantees for function arguments: not re-fetched in this session.
- Whether the Agents SDK auto-retries the model on JSON parse errors in tool arguments: not found.

---

## 6. Loop/stall detection and progress tracking

### Takeaway
The most concrete documented mechanism is Codex Goals:
- each continuation turn is classified as "progress / verified wait / no progress"
- automatic continuation is suppressed after a turn with no tool call ("so Codex does not spin")
- a goal may be marked `blocked` only after the same blocking condition repeats for at least 3 consecutive goal turns
- a strict evidence-based completion audit runs before marking a goal complete
- `update_plan` keeps progress visible

The Agents SDK only prevents forced tool-choice loops, via `reset_tool_choice`.

### Cited Findings
- **No-progress check (verbatim).** "Classify the previous goal turn as progress, a verified wait, or no progress. Progress changes authoritative state, completes work, or yields evidence that changes the next action; status restatements and unexecuted plans are no progress." Also: "Treat equivalent blockers as the same condition across turns even when their wording or stated next step changes." — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Blocked audit.** "Do not call update_goal with status 'blocked' the first time a blocker appears. Only use status 'blocked' when the same blocking condition has repeated for at least three consecutive goal turns… Never use status 'blocked' merely because the work is hard, slow, uncertain, incomplete…" — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Completion audit.**
  - "treat completion as unproven". For every explicit requirement, identify authoritative evidence and inspect current state.
  - "Treat uncertain or indirect evidence as not achieved". "The audit must prove completion, not merely fail to find obvious remaining work."
  - "Do not mark a goal complete merely because the budget is nearly exhausted."
  - Source: [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Anti-scope-shrinking.** "Keep the full objective intact… do not redefine success around a smaller or easier task." "Do not substitute a narrower, safer, smaller… solution because it is more likely to pass current tests." — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Spin guard.**
  - "If a continuation turn makes no tool call, the next automatic continuation is suppressed so Codex does not spin."
  - Continuation is checked only at safe boundaries after a turn. Plan-only work does not trigger continuation.
  - Source: [Goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
- **Progress visibility.** "If update_plan is available and the next work is meaningfully multi-step, use it to show a concise plan tied to the real objective… do not treat a plan update as a substitute for doing the work." — [continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)
- **Agents SDK loop guards.**
  - `reset_tool_choice` defaults to `True`, which resets `tool_choice` to "auto" after a tool call. This prevents an infinite loop in which a forced `tool_choice` makes the model call the tool again and again.
  - `tool_use_behavior` (`"stop_on_first_tool"`, `StopAtTools`, or a custom function) can end runs on specific tool calls.
  - Source: [Agents docs](https://github.com/openai/openai-agents-python/blob/main/docs/agents.md)
- **ChatGPT agent.** The system card hub does not describe task duration caps or a general loop or stall mechanism. — [Deployment Safety Hub](https://deploymentsafety.openai.com/chatgpt-agent/watch-mode)

### Inferences
- The Codex rules are a ready-made design for replacing "24 steps × 5 chunks":
  - Keep going while each turn makes progress or is a verified wait.
  - Count consecutive turns with no progress or the same blocker, and stop as "blocked" at 3, asking the user.
  - Stop as "complete" only after an evidence audit, and as "budget_limited" with a wrap-up summary.
  - Classify blockers semantically, so the same blocker described in different words still counts as a repeat.

### Gaps
- No OpenAI documentation of action-level repetition detection (for example, identical click/URL sequences) for ChatGPT agent or Operator was found.

---

## 7. Procedural memory and reuse of what worked

### Takeaway
ChatGPT memory was disabled in agent mode at launch to limit prompt-injection exfiltration. Codex uses three mechanisms for reuse:
- AGENTS.md project instructions (`project_doc_max_bytes`, fallback filenames)
- Skills, whose catalog gets 2% of the context window by default
- an off-by-default `features.memories` system that generates and consolidates memories from past rollouts

Goals are explicitly thread state, not global memory.

### Cited Findings
- **ChatGPT agent memory.** Memory is disabled at launch to reduce the risk of prompt injections exfiltrating remembered data, and "may be revisited". — [Deployment Safety Hub](https://deploymentsafety.openai.com/chatgpt-agent/watch-mode)
- **Codex AGENTS.md.** `project_doc_max_bytes` sets the maximum bytes read from AGENTS.md. `project_doc_fallback_filenames` lists alternates. — [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Codex Skills.**
  - `skills.max_context_tokens` defaults to 2% of the model context window, with explicit values capped at 10,000.
  - `skills.config[]` provides per-skill enablement.
  - `skill_approval` is a granular approval key.
  - A sample `skill-installer` skill ships in `codex-rs/skills/src/assets/samples/`.
  - Source: [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference), [codex repo](https://github.com/openai/codex/tree/main/codex-rs/skills/src/assets/samples)
- **Codex memories (`features.memories`, off by default).** Settings and defaults:

  | Setting | Default | Allowed range |
  |---|---|---|
  | `generate_memories` | true | |
  | `use_memories` | true | |
  | `disable_on_external_context` | false | |
  | `max_rollout_age_days` | 30 | 0–90 |
  | `max_rollouts_per_startup` | 16 | up to 128 |
  | `max_raw_memories_for_consolidation` | 256 | up to 4096 |
  | `max_unused_days` | 30 | |
  | `min_rollout_idle_hours` | 6 | |
  | `min_rate_limit_remaining_percent` | 25 | |

  Memories are mined from past rollouts and consolidated. — [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Goals are not memory.** Goals are "persisted thread state, not as global memory and not as project-level instructions". — [Goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)

### Inferences
- OpenAI keeps three things separate:
  1. per-task persistent state (the goal and its budget)
  2. per-project static procedure (AGENTS.md and skills, loaded under a small token budget)
  3. cross-session learned memory (Codex memories, consolidated offline from idle rollouts and turned off for untrusted contexts)

  The `disable_on_external_context` flag and ChatGPT agent's memory-off stance suggest that a browser bot reading untrusted web pages should gate any learned procedural memory carefully.

### Gaps
- How Codex memories are summarized and consolidated (prompts, storage format): not inspected.
- Whether ChatGPT agent memory has been re-enabled since launch: not verified (help center 403).
- Whether Operator or ChatGPT agent has any "saved task" or procedural reuse feature beyond scheduled tasks: not verified.
