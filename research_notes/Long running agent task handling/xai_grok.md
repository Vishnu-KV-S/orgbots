# xAI Grok: mechanisms for long-running, multi-step agent work (as of Oct 2026)

Scope note: Primary sources are docs.x.ai and x.ai/news. Several fetched xAI doc pages now refer to the company as "SpaceXAI", so treat that name as the same publisher. Current flagship API model in the docs is `grok-4.7` (500k context). There are three distinct agent surfaces, and the answers differ for each:
(a) the **API's server-side agentic tool loop** (Responses API / xAI SDK, `max_turns`);
(b) the **multi-agent model** `grok-4.20-multi-agent` (API) plus the consumer Heavy, DeepSearch and DeeperSearch modes;
(c) **Grok Bot** (launched Aug 11, 2026), a computer-use agent with a persistent cloud computer, skills, routines, memory and takeover. Of the three, it is the closest analogue to our browser bot.

---

## Q1. What limits apply to agentic runs, and what happens when they are hit?

### Takeaway
The API caps a run by **assistant turns**, not tool calls (`max_turns`). If it is unset, an undisclosed server default applies. When the cap is hit, the agent **stops calling tools and writes a final answer from what it has gathered**; the run does not error. Calls to client-side tools end the request and **reset the turn counter** on the follow-up request. Grok Bot publishes no per-task step or time limit. It is bounded by weekly usage and spending limits, plus routine limits (50 routines per Bot, schedules at least 5 minutes apart).

### Cited Findings
- `max_turns` "does not directly limit the number of individual tool calls"; it limits assistant turns in the agentic loop. One turn means the model analyzes context, calls one or more tools (possibly in parallel), gets the results, and processes them. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Suggested values: 1–2 for quick lookups, 3–5 for balanced research, and 10+ or unset for deep research, trading latency and cost for thoroughness. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- If `max_turns` is unset, "the server applies a global default cap" (no number published). When the cap is reached, the agent stops making tool calls and generates a final response from the information gathered so far. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- `max_turns` "only limits the assistant/server-side tool call turns within a single request." When the model calls a client-side tool, the request ends and the follow-up request starts with a fresh `max_turns` count. Doc example: with `max_turns=5`, 3 server-side calls followed by a client call still leave the follow-up 5 more turns. — [xAI Advanced tool usage](https://docs.x.ai/docs/guides/tools/advanced-usage)
- Billing has two parts: tokens plus tool invocations. Costs "scale with complexity" because a single query may make several tool calls. — [xAI Tools overview](https://docs.x.ai/docs/guides/tools/overview)
- Only **successful** server-side tool executions are billed. `tool_calls` lists every attempted call, including failures; `server_side_tool_usage` counts successes per category (e.g. `SERVER_SIDE_TOOL_WEB_SEARCH`, `SERVER_SIDE_TOOL_X_SEARCH`, `SERVER_SIDE_TOOL_CODE_EXECUTION`, `SERVER_SIDE_TOOL_MCP`). Since Sept 21, 2026, X Search is billed per post and per user fetched (`x_posts_fetched`, `x_users_fetched`). — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Token accounting across the loop: `prompt_tokens` is cumulative across every inference step in the loop ("each request includes the full history, so this grows with each step"). `completion_tokens` counts only the final text. Reasoning tokens are counted separately. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Multi-agent model: "The `max_tokens` parameter is not currently supported by the multi-agent model variant". Client-side function calling and custom tools are also not supported (built-in tools and remote MCP only). — [xAI Multi Agent](https://docs.x.ai/developers/model-capabilities/text/multi-agent)
- grok-4.7 page: "No text output limit"; 500,000-token context. — [Grok 4.7 Developer Guide](https://docs.x.ai/developers/grok-4-7)
- Long-context pricing cliff: once a request's prompt reaches 200k tokens, the higher rate applies to **all** tokens in that request. grok-4.7 costs $2/$6 per M (input/output) below 200k and $4/$12 at or above; cached input is $0.50 or $1.00. — [xAI Models & pricing](https://docs.x.ai/developers/models)
- Grok Bot: paid plans "include weekly usage", and eligible accounts can add on-demand usage billed from model and token cost. When usage runs out or an on-demand spending limit is hit, work and routines stop until billing is addressed. — [Grok Bot FAQ](https://docs.x.ai/grok-bot/faq); [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)
- Grok Bot docs give **no turn, time or step limit** per task. The launch post says Bots "keep working 24/7." — [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting); [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot)
- Grok Bot routine limits: up to 50 routines per Bot, the 20 most recent run records kept per routine, schedules at least 5 minutes apart, and teaching recordings of up to 10 minutes. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Third-party integrations name the limit differently. For example, visionagents.ai exposes `tools_max_rounds` (default 3). — [visionagents.ai xAI integration](https://visionagents.ai/integrations/xai) (third-party)

### Inferences
- xAI's documented stop behavior is **graceful degradation**: hitting the cap triggers a forced final synthesis turn instead of an error. That is a direct pattern for replacing a hard stop at "24 steps x 5 chunks": on the last allowed step, disable tools and force a "summarize progress and answer or hand back" turn.
- The counter reset on client-tool pauses means xAI treats each request as a **budgeted segment** of a longer run, which resembles our "chunks". The difference is that the run's total length is set by whoever drives the outer loop, not by a fixed product of constants.
- The 200k pricing cliff, together with cumulative `prompt_tokens`, gives a cost reason to compact well before the window fills.

### Gaps
- The numeric default for `max_turns` is not published.
- No documented wall-clock timeout for a single agentic Responses request.
- No documented per-run token or dollar budget parameter (other than `max_tokens` on single-agent models).
- Grok Bot's internal step or time caps are not documented.

---

## Q2. How is context managed over long agentic runs?

### Takeaway
xAI provides four documented mechanisms:
1. **Server-side state** via `store_messages` / `previous_response_id`, with stored responses kept for 30 days.
2. **Encrypted carry-over**: reasoning and tool state are returned as opaque blobs and passed back unchanged.
3. An **explicit, client-triggered context compaction endpoint**, `POST /v1/responses/compact`. It is not automatic; xAI suggests calling it every N turns.
4. **Prefix-based prompt caching**, with sticky routing through `prompt_cache_key` / `x-grok-conv-id`.

Grok Bot additionally keeps **summaries of its work** as memory rather than replaying full history.

### Cited Findings
- Setting `store_messages=True` stores the full history server-side, including reasoning, server-side tool calls and responses. Passing `previous_response_id` continues the conversation "fully hydrated with the prior agentic state", and the follow-up may change tools or model parameters. — [xAI Advanced tool usage](https://docs.x.ai/docs/guides/tools/advanced-usage)
- Stored responses are kept for 30 days and then deleted. — [xAI Responses API reference](https://docs.x.ai/developers/rest-api-reference/inference/responses) (from search snippet; page not opened in full)
- For ZDR (zero data retention), `use_encrypted_content=True` returns the full history to the client with reasoning and tool outputs encrypted, which the client appends before the next call. The OpenAI SDK equivalent is `include=["reasoning.encrypted_content"]`. — [xAI Advanced tool usage](https://docs.x.ai/docs/guides/tools/advanced-usage)
- grok-4.7: "Encrypted reasoning is always returned on the Responses API"; "Pass the reasoning items back unchanged in the next request's `input`." The guide states that "Long agent loops additionally benefit from context compaction." — [Grok 4.7 Developer Guide](https://docs.x.ai/developers/grok-4-7)
- **Context Compaction** (released May 2026 per release notes):
  - `POST /v1/responses/compact` turns a long conversation into "a single opaque item" of `type: "compaction"`.
  - It keeps "system prompts, attached files, prior reasoning, and a compacted record of the turns" and drops verbose tool output and back-and-forth.
  - The item goes at the head of the next request's input; new turns are appended after it and never before; the output must not be pruned or edited.
  - Re-compacting later is allowed, and each request gets one compaction pass.
  - It "cannot rescue a request past `context_length_exceeded`".
  - Triggering is done by **your code**: "every N turns inside an agent loop" or when your own bookkeeping crosses a threshold.
  - xAI advises "Pick a smaller / faster model for compaction if you are doing it frequently."
  - `usage.dropped_message_count` reports how many messages were folded in (example: 45).
  - The xAI SDK has `client.chat.compact_context(...)` and `chat.compact()`. — [xAI Context Compaction](https://docs.x.ai/developers/advanced-api-usage/context-compaction); [xAI Release notes](https://docs.x.ai/developers/release-notes)
- The compaction docs describe **no automatic or server-side compaction** and no threshold parameter. — [xAI Context Compaction](https://docs.x.ai/developers/advanced-api-usage/context-compaction)
- Prompt caching:
  - Caching works from the start of the messages array, so the longest matching prefix is served from cache and "Cached tokens are billed at a reduced rate".
  - Entries "can be evicted due to memory pressure", and no TTL is published.
  - Best practices: append only, never edit or reorder earlier messages, and put static content first. — [xAI Prompt caching: how it works](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/how-it-works); [Maximizing cache hits](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits)
- Caches live on individual servers. Use `prompt_cache_key` (Responses API) or the `x-grok-conv-id` header (Chat Completions) to route a conversation to the same server. grok-4.7 guide: "without it you often pay full input price on a cache-cold server." — [Grok 4.7 Developer Guide](https://docs.x.ai/developers/grok-4-7); [Maximizing cache hits](https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits)
- Cached input is about 4x cheaper than fresh input on grok-4.7 ($0.50 vs $2.00 per M). — [xAI Models & pricing](https://docs.x.ai/developers/models)
- Agentic loops cache well "because most of the prompt stays unchanged between steps" (`cached_prompt_text_tokens`). — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Server-side tool call **outputs are not returned** in the API response; only the invocations are. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Context windows: grok-4.7, 4.6 and 4.5 have 500k; grok-4.3 and grok-4.20 (including multi-agent) have 1M; grok-build-0.1 has 256k. — [xAI Models & pricing](https://docs.x.ai/developers/models)
- Grok Bot memory: "A Bot can retain stable working preferences, important facts, and summaries from its work. This helps it keep a role over time without replaying every prior message." — [Grok Bot docs (via search snippet)](https://docs.x.ai/grok-bot/overview); [Grok Bot FAQ](https://docs.x.ai/grok-bot/faq)
- Grok Build (coding CLI) subagents are "independent child sessions with their own context" that return **a summary** to the parent when finished. — [Grok Build Subagents](https://docs.x.ai/build/features/subagents)
- Third-party caveat: an apiyi.com A/B test reportedly found no measurable cache hit-rate difference with or without `x-grok-conv-id`. This contradicts xAI's recommendation and is unofficial. — [apiyi docs](https://docs.apiyi.com/en/api-capabilities/grok/prompt-caching)

### Inferences
- xAI's recipe for long loops is: append-only history, a stable cache key, encrypted reasoning passed back verbatim, and **periodic compaction into one summary item placed at the head** of context, often run by a cheaper model. For a DeepSeek-based LangGraph bot this maps to: keep the message prefix stable for DeepSeek's prefix cache, and every N steps replace older steps with a summary block covering task, progress, key facts and open threads, rather than ending the run.
- Isolating sub-tasks in child contexts that return only summaries (Grok Build subagents, multi-agent sub-agent state hidden by default) is a second context-control tool.

### Gaps
- How server-side tool loops manage context inside a single request (for example, whether large tool outputs are truncated internally) is not documented.
- No cache TTL or minimum cacheable length is published.
- Grok Bot's internal compaction or summary policy is not documented beyond the memory description.

---

## Q3. How are slow or long jobs handled (async, deferred, streaming, scheduled)?

### Takeaway
On the API: **deferred completions**, where you get a `request_id` and poll for up to 24h, and the result can be fetched once; **verbose streaming** of tool calls as they happen; and stored responses you resume by ID. On the consumer and product side: **Grok Bot background turns** on a persistent cloud computer that keep running when the client disconnects, with a live preview of clicks, typing and status; **routines** (scheduled or event-triggered); and Grok **Tasks**, later **Automations**, for scheduled prompts.

### Cited Findings
- Deferred completions: POST `/v1/chat/completions` with `"deferred": true` returns a `request_id`. GET `/v1/chat/deferred-completion/{request_id}` returns `202 Accepted` (empty body) until ready, then `200`. The result "would be available to be requested exactly once within 24 hours." The rate limit is the same as for chat completions. The SDK's `chat.defer(timeout=10 min, interval=10 s)` is a client-side polling default. — [xAI Deferred chat completions](https://docs.x.ai/docs/guides/deferred-chat-completions)
- Whether deferred mode works with agentic or server-side tools is **not stated**. — [xAI Deferred chat completions](https://docs.x.ai/docs/guides/deferred-chat-completions)
- Streaming: each tool-call decision is surfaced on `chunk.tool_calls` "as it happens", and `include=["verbose_streaming"]` shows server-side tool calls live. Tool **outputs** are not streamed back. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details); [xAI Advanced tool usage](https://docs.x.ai/docs/guides/tools/advanced-usage)
- Grok Bot: "Bot work runs on the cloud computer. Closing the app, laptop, or phone does not stop a background turn or routine." — [Grok Bot FAQ](https://docs.x.ai/grok-bot/faq)
- Grok Bot computer preview "shows clicks, typing, navigation, and current status"; users can leave the preview while work continues. — [Grok Bot Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)
- Concurrency:
  - All Bots on an account share **one** cloud computer, including browser cookies, sessions, files and CLI credentials.
  - Each Bot has its own screen, and "one Bot can run only one computer-use task on its screen at a time."
  - Screens are "separate work surfaces, not separate security boundaries."
  - Durable files belong in `/workspace`.
  - Reset restores the last snapshot and "can lose recent or unsynced work." — [Grok Bot Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps); [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)
- Routines: a routine "tells one Bot when to run a workflow—on a schedule or, where supported, after an event" (e.g. Slack or GitHub events via Cursor integrations). Run history shows recent successes and failures. Unattended routines may be paused if the Bot asks whether to keep running them after a long absence and gets no answer. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Grok Tasks (consumer, **press-reported only**):
  - Scheduled prompts that run at a set time (one-time, daily, weekly, monthly or annual), with results delivered by notification or email. — [TestingCatalog](https://www.testingcatalog.com/upcoming-groks-tasks-feature-gets-ui-upgrade-and-scheduling-tweaks.md); [AIbase](https://news.aibase.com/news/18990)
  - AIbase reported free-tier quotas of 3 tasks per day and 10 temporary tasks per week (unverified, possibly outdated). — [AIbase](https://news.aibase.com/news/18990)
  - Tasks were reportedly folded into **Automations** (launched July 16, 2026), which add email/event triggers, skill selection, a model picker, connectors and run-history logs. — [Blockchain.news](https://blockchain.news/news/grok-automations-scheduled-tasks); [MindStudio](https://mindstudio.ai/blog/grok-automations-scheduled-tasks-email-triggers) (secondary sources; no xAI primary page found)

### Inferences
- The Grok Bot design separates the agent's lifetime from the client connection: a persistent machine, durable workspace files, a live status preview, and resumable conversations. For our bot this argues for a durable run record (persisted checkpoints) and resuming from the last checkpoint rather than a single long synchronous invocation.

### Gaps
- No xAI primary documentation found for consumer Grok Tasks or Automations limits.
- No documented API "background mode" for Responses (beyond deferred chat completions and stored responses).
- No documented heartbeat or progress-event schema for long server-side tool loops beyond tool-call chunks.

---

## Q4. How do DeepSearch, DeeperSearch and Heavy decompose and parallelize work?

### Takeaway
xAI's primary sources describe Heavy as **parallel test-time compute**, with multiple agents exploring hypotheses at once and then comparing results. The API exposes this as `grok-4.20-multi-agent` with **4 or 16 agents** and a **leader agent** that synthesizes. Sub-agent internals are hidden or encrypted. xAI does **not** document its decomposition or verification algorithm. DeepSearch is documented only at a high level ("reason about conflicting facts"); DeeperSearch only in press.

### Cited Findings
- Grok 4 Heavy (July 9, 2025) uses "parallel test-time compute, which allows Grok to consider multiple hypotheses at once." The illustration shows three agents each "Thought for 10 minutes." — [xAI: Grok 4](https://x.ai/news/grok-4)
- Musk (launch stream, as reported) said Heavy spawns multiple agents that work independently, then "compare notes"; it is not a simple majority vote, because often only one agent finds the key insight and shares it with the others. — [OfficeChai](https://officechai.com/miscellaneous/elon-musk-explains-how-grok4-heavy-xais-multi-agent-reasoning-model-works/) (press report of spoken remarks)
- API multi-agent (beta), `grok-4.20-multi-agent`:
  - Agents specialize in searching, analyzing and synthesizing, and a **leader agent** "synthesizes the sub-agents' discussion and returns the final answer."
  - Two configurations: 4 agents (`agent_count=4` or `reasoning.effort` low/medium) and 16 agents (`agent_count=16` or effort high/xhigh). The 16-agent setup "uses significantly more tokens."
  - Built-in tools (`web_search`, `x_search`, `code_execution`, `collections_search`, remote MCP) run in a server-side agent loop.
  - Only the leader's tool calls and final response are returned. Sub-agent state is encrypted and included only with `use_encrypted_content=True`, to preserve multi-agent context across turns.
  - All agents' tokens and tool calls are billed. — [xAI Multi Agent](https://docs.x.ai/developers/model-capabilities/text/multi-agent)
- The multi-agent model works only with the xAI SDK or Responses API, not Chat Completions. Rate limits listed: 9 rps and 2.5M tokens per minute. — [xAI Multi Agent](https://docs.x.ai/developers/model-capabilities/text/multi-agent); [Grok 4.20 Multi Agent model page (via search)](https://docs.x.ai/developers/models/grok-4.20-multi-agent)
- Third-party claims, **not confirmed by xAI**: a coordinator analyzes complexity, splits sub-tasks, dispatches them in parallel, and resolves disagreements. Named community roles: "Harper" (research), "Benjamin" (logic), "Lucas" (contrarian). The same source says xAI has not documented its arbitration. — [Verdent guide](https://www.verdent.ai/guides/grok-4-20-multi-agent-system)
- DeepSearch (Feb 19, 2025) was xAI's "first agent", designed to "synthesize key information, reason about conflicting facts and opinions"; "Its final summary trace results in a concise and comprehensive report." Grok 3 Think "can spend anywhere from a few seconds to several minutes reasoning." — [xAI: Grok 3](https://x.ai/news/grok-3) (older, 2025)
- DeeperSearch (March 2025, **press only**) is a slower mode that runs more iterations. Reported run times range from about 6.5 minutes to over 30 minutes; one test took 37m37s with 46 sources. Reported weaknesses include reliance on X sources and difficulty accessing mainstream news. — [The Decoder](https://the-decoder.com/?p=22232); [Leptidigital](https://www.leptidigital.fr/intelligence-artificielle-ia/grok-deepersearch-recherche-ia-avancee-plus-lente-75287/); [BAAI hub](https://hub.baai.ac.cn/view/44299)
- The Grok Bot launch describes running several Bots in parallel with a "chief of staff" Bot overseeing specialists. Bots message each other, share context in threads, hand off work and "nudge a stalled handoff." — [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot)

### Inferences
- xAI's pattern is parallel independent attempts plus leader synthesis, with costs multiplying linearly in agent count. For a browser bot (one screen, so actions are serial), parallelism fits best for **research sub-questions or verification** rather than concurrent UI actions. Grok Bot also limits each screen to one computer-use task.

### Gaps
- The verification and arbitration method between agents is undocumented by xAI.
- How Heavy decomposes a task (role assignment, sharing protocol) is undocumented.
- The current state of the consumer DeepSearch/DeeperSearch modes in Oct 2026 is unknown; no 2026 primary source was found.

---

## Q5. Memory, personalization, and reusing what worked across sessions

### Takeaway
Consumer Grok has had cross-chat memory since April 2025, with user-visible references and per-memory deletion. Grok Bot adds per-Bot memory of preferences, facts and **work summaries**, plus **skills**: reusable procedures created by saving a completed task or by recording a demonstration of up to 10 minutes. Skills are explicitly drafts that need failure handling and approval rules added. xAI recommends a "one-time task, then make it reliable, then save as skill, then automate as routine" progression and warns that memory is not authoritative.

### Cited Findings
- Grok memory (April 2025, beta on grok.com and iOS/Android; not in the EU or UK at launch): it remembers details from past chats for tailored answers. Users can toggle "Personalize with Memories" under Data Controls and delete individual memories. Private Chat avoids storage. — [The Decoder](https://the-decoder.com/xai-adds-memory-feature-to-grok-chatbot-for-personalized-responses/); [Yahoo/TechCrunch](https://finance.yahoo.com/news/xai-adds-memory-feature-grok-021115965.html) (older, 2025)
- Grok Bot memory is kept "per Bot": "Its conversation and learned role are separate from other Bots, while shared files, browser sessions, group messages, and direct handoffs can move context between them." Advice: "Ask the Bot to cite or reopen current data for consequential decisions." — [Grok Bot docs (via search snippet)](https://docs.x.ai/grok-bot/overview); [Grok Bot FAQ](https://docs.x.ai/grok-bot/faq)
- Skills:
  - A skill is "a reusable set of instructions for how to do a task." It should cover when to use it, inputs and access, the sequence of steps, validation, what to return, and what needs approval.
  - Private skills form one library shared by all of a user's Bots. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Teach a task: records visible computer interaction for up to 10 minutes. "The learned skill is a draft. Add decision rules, failure handling, and approval boundaries that may not be obvious from one example." It may roll out gradually. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Recommended progression: start with a one-time task, make it reliable, save the method as a skill, and only then automate it as a routine. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Launch post: a Bot can watch you do a job, save the steps as a routine, "applies your corrections", and later runs the process itself. Bots "remember conversations" and can resume earlier threads. — [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot)
- User-written GTM guide on x.ai: "every time I steer my agent I have it add to my skill" (anecdotal, not official docs). — [Grok Bot for GTM guide](https://x.ai/bot/guides/grok-bot-for-gtm)
- Reliability caveat: a memory-plugin vendor blog reports that consumer memory continuity is inconsistent across platforms. The source has a commercial interest. — [MemoryPlugin blog](https://blog.memoryplugin.com/how-grok-memory-works/)

### Inferences
- The concrete "reuse what worked" mechanism is **procedural memory as editable skill documents**: steps plus validation, failure policy and approval points, captured after a successful run or from user corrections. That suits a browser bot, for example storing per-site playbooks after a successful completion.

### Gaps
- Whether skills update automatically from corrections is unclear. The docs describe saving and editing; automatic correction-folding appears only in the launch post and a user guide.
- No documentation of how memory items are extracted or stored, or of any retrieval mechanism.

---

## Q6. Malformed outputs, stuck loops, and human steering

### Takeaway
xAI documents **human-in-the-loop** controls for Grok Bot in detail:
- redirect messages and "Stop now";
- computer **takeover** for credentials, 2FA, CAPTCHA and payments;
- approval cards with "Ask first" rules;
- pause and notify on session expiry;
- retry from the error state.

On the API side, failed tool calls are simply recorded and not billed. **No documented loop detection, malformed-tool-call repair, or automatic retry policy** was found anywhere.

### Cited Findings
- Failed tool executions, invalid parameters and network errors make `tool_calls` (all attempts) differ from `server_side_tool_usage` (successes). Only successes are billed. — [xAI Tool usage details](https://docs.x.ai/developers/tools/tool-usage-details)
- Mixed client/server tools: when the model calls a client-side tool, execution pauses and control returns to the app, which runs the tool, appends the result and sends a new request. That return of control is a natural point for validation or steering. `get_tool_call_type()` distinguishes client from server calls. — [xAI Advanced tool usage](https://docs.x.ai/docs/guides/tools/advanced-usage)
- Grok Bot stuck-Bot guidance: check the conversation status and open the computer to look for "a pending question, approval, login, CAPTCHA, or secret request." "A computer-use task already active on that Bot's screen may need to finish or be redirected before another one can start." — [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)
- Steering: send "a short redirect if the current approach is wrong", or a direct "Stop now"; reject or cancel approval cards, or send a replacement instruction. — [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)
- Takeover: for passwords, passkeys, 2FA, CAPTCHAs, payment or identity checks, "take control, complete only the blocked step, and tell the Bot to continue". Don't paste secrets into chat; use masked secret fields. If a site expires a session or asks for verification again, the Bot should "pause and notify you" rather than bypass the check. — [Grok Bot Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)
- Approvals: "Sensitive or consequential actions can stop for approval," configured through auto-review and narrow "Ask first" rules (send, publish, delete, purchase, change production). Test runs should confirm the routine stopped at the intended approval point. — [Grok Bot FAQ](https://docs.x.ai/grok-bot/faq); [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Launch post: Bots return "only when they need your approval or a judgment call" and "learn when to check in and when to keep going." — [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot)
- Failure policy guidance for routines:
  - "If the source data is unavailable, report the failure instead of using old data."
  - Include no-data and stale-data policies.
  - Make retries idempotent.
  - Say where to report partial completion.
  - Re-test after a site or format changes. — [Grok Bot Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- Recovery ladder for an unreachable computer: retry, restart the app, "Recover computer", update, wait, and reset only as a last resort. Recover and Update preserve durable files and logins. — [Grok Bot Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)

### Inferences
- xAI's approach to "stuck" is **status visibility plus cheap human redirection**: a live preview, pending-question surfacing, and mid-run redirect messages. Automated loop-breaking is not documented. For our bot this suggests:
  - surfacing a "blocked on X" state and pausing rather than burning steps;
  - accepting user redirect messages between steps;
  - an explicit pause-for-human state for auth, CAPTCHA and payment.

### Gaps
- No xAI documentation of loop or repetition detection, malformed JSON/tool-call repair, or automatic retries in the server-side agent loop.
- How mid-run messages to a busy Grok Bot are queued or injected is not specified (redirects are recommended, but the mechanics are undocumented).
- The model powering Grok Bot is not named in the launch post or the FAQ.
