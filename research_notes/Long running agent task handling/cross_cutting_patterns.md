# Cross-cutting patterns for long-horizon web/browser/computer-use agents

Scope: techniques from production agents (browser-use, Anthropic computer use and harness guidance, Manus, OpenHands, Agent-E) and research (AWM, SkillWeaver, ExpeL, Agent KB, WebArena and related work). OpenAI, xAI and LangGraph specifics are excluded. Researched 2026-10-09. browser-use and OpenHands behavior was read directly from current `main` source, not only from docs.

## 1. Replacing fixed step caps: progress tracking, stall/loop detection, budgets, completion checks, asking the human

### Takeaway
Production agents do not stop at a small fixed number of steps. They use a large hard ceiling (browser-use defaults to `max_steps=500`) and add soft controls on top:
- loop and stagnation detectors that inject escalating nudges;
- a replan prompt after N consecutive failures;
- a budget warning at 75% of the budget, then a forced "done-only" final step;
- an LLM judge that checks the trajectory afterwards;
- persistent progress or feature files, so "done" is judged against an explicit checklist instead of the model's feeling.

Hard stops are kept for clear pathologies, such as the same call failing with the same error.

### Cited Findings
**browser-use (current main source)**
- **Step ceiling.** `Agent.run(max_steps: int = 500)` is the default ceiling. — [browser-use service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py)
- **Budget warning.** Once ≥75% of `max_steps` is used, browser-use injects: "BUDGET WARNING: You have used X/Y steps … If the task cannot be completed in the remaining steps, prioritize: (1) consolidate your results (save to files…), (2) call done with what you have. Partial results are far more valuable than exhausting all steps with nothing saved." — [browser-use service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py)
- **Forced final step.** On the last step the action schema is rebuilt to contain only `done`, and the agent is told: "You reached max_steps - this is your last step. Your only tool available is the 'done' tool." The model therefore always produces a final answer or partial result instead of being cut off. — [browser-use service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py)
- **`ActionLoopDetector` (enabled by default, window of 20 actions).** It is "a soft detection system — it generates context messages for the LLM but never blocks actions." — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)
  - It hashes normalized actions:
    - search: lowercased, sorted, deduplicated tokens;
    - click: element index;
    - input: index plus lowercased, stripped text;
    - navigate: full URL ("navigating to different paths is genuine exploration");
    - scroll: direction plus index;
    - anything else: action name plus sorted params.
  - Nudges escalate when one hash appears ≥5, ≥8 and ≥12 times in the window. Wording goes from "If this is intentional and making progress, carry on" to "a different approach might get you there faster."
- **Page-stagnation detection.** Each step records a `PageFingerprint` (URL, interactive element count, SHA-256 prefix of the DOM text). After ≥5 consecutive identical fingerprints, the agent is told: "The page content has not changed across N consecutive actions. Your actions might not be having the intended effect." — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)
- **Planning (on by default).** The agent keeps a `plan: list[PlanItem]` where each item has a status of pending, current, done or skipped. Two nudges support it:
  - `planning_replan_on_stall=3`: after 3 consecutive failures it injects "REPLAN SUGGESTED … Output a new `plan_update`."
  - `planning_exploration_limit=5`: after 5 steps without a plan it injects "PLANNING NUDGE … output a `plan_update` with clear todo items now. If the task is already done or nearly done, call `done` instead."
  - Source: [browser-use service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py), [views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)
- **Failure handling.**
  - `max_failures` (default 5) counts consecutive step errors.
  - `final_response_after_failure=True` forces one last model call with intermediate output after max_failures.
  - `fallback_llm` switches to a backup model for the rest of the run once the primary's retries are exhausted.
  - `step_timeout` defaults to 180 s.
  - Source: [browser-use docs](https://docs.browser-use.com/customize/agent/all-parameters)
- **Judge (`use_judge=True` by default).** An LLM judge reviews the task, final result, step log and up to the last 10 screenshots. It returns `verdict`, `failure_reason`, `impossible_task` (vague instructions, broken site, missing login, etc.) and `reached_captcha`. — [browser-use judge.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/judge.py), [views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)

**OpenHands `StuckDetector`**
- It checks five scenarios over the events since the last user message (at most 20 events scanned):
  1. the same action and observation repeated (threshold 4);
  2. the same action producing errors (threshold 3);
  3. an agent monologue, meaning repeated messages with no user input (threshold 3);
  4. an alternating A-B-A-B action/observation pattern (threshold 6);
  5. a context-window-error loop (checked once there are ≥10 events).
  - Source: [OpenHands SDK stuck_detector.py](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py), with defaults in `StuckDetectionThresholds` (conversation/types.py, same repo)
- When an error streak reaches the threshold, it sends a nudge once before declaring the agent stuck: "You've called `{tool}` with the same arguments {N} times in a row and gotten the same error each time: {error}. Repeating the exact same call again will not work — review the error message and either correct the arguments or try a different approach." — [OpenHands SDK stuck_detector.py](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)

**Other loop-detection guidance**
- OpenRouter's agent SDK has "doom-loop detection", off by default. Its recommended staged thresholds are observe at 2 repeats, block at 3 and stop at 6. — [OpenRouter docs](https://openrouter.ai/docs/agent-sdk/call-model/doom-loop-detection.md)
- A practitioner guide (secondary, not peer-reviewed) argues that a repeated call is not always a loop: re-searching with a changed query is legitimate. It recommends combining normalized arguments, unchanged results and unchanged task state. It also recommends separate limits for total actions, retries, wall-clock time and cost. — [OneUptime blog](https://oneuptime.com/blog/post/2026-09-12-detect-tool-loops-dead-ends-production-agents/markdown)
- A Cursor user reported that repeated screenshots during browser work falsely triggered loop detection. Observation-only actions need to be excluded or treated specially (anecdotal). — [Cursor forum](https://forum.cursor.com/t/how-to-disable-loop-detection/142696)

**Anthropic harness and computer-use guidance**
- **Two-agent harness for multi-session work.** An initializer agent writes `init.sh`, a progress log and a JSON feature list (over 200 features for a claude.ai clone), each with `passes: false`. Later "coding agent" sessions follow a fixed routine:
  - read the git log and progress file;
  - pick the highest-priority unfinished feature;
  - run a basic end-to-end test first;
  - work on only one feature per session;
  - commit and write a progress summary.
  - Source: [Anthropic – Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **Format and permissions for the feature list.** JSON was chosen because the model is "less likely to inappropriately change or overwrite" it than Markdown. Agents may only flip `passes`, and "It is unacceptable to remove or edit tests." — [Anthropic harness post](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **Failure modes Anthropic names, with fixes.**

  | Failure | Fix |
  |---|---|
  | Declaring victory too early | The feature list, plus one feature per session |
  | Marking features done prematurely | Self-verification before marking `passes` |
  | Unit-test/curl-only checks | Explicitly prompting browser automation (Puppeteer MCP) so it tests "as a human user would" |

  The post reports no success rates or benchmarks (qualitative only). — [Anthropic harness post](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- **Check outcomes, don't assume them.** Computer-use docs say: "Claude sometimes assumes outcomes of its actions without explicitly checking their results." The recommended prompt: "After each step, take a screenshot and carefully evaluate if you have achieved the right outcome. State in one sentence what the screenshot shows and whether the step succeeded. If it didn't, try again." — [Claude computer use tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- **Dropdowns and menus.** The same docs say: "Some UI elements (such as dropdowns and scrollbars) might be tricky for Claude to manipulate using mouse movements… try prompting the model to use keyboard shortcuts." This bears directly on menu loops. — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- **Loop limits and human confirmation.** The docs' reference loop uses `max_iterations=10` purely as a cost safeguard. They recommend human confirmation for actions with "meaningful real-world consequences" (financial transactions, accepting terms, cookies). For batch actions, the check should run before each block. — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)

**Other systems and research**
- **OpenHands critic model.** Re-running with a critic that selects among attempts raised SWE-bench Verified from 60.6% (1 rollout) to 66.4% (5 rollouts). This is evidence that a separate verifier/selector adds measurable value. — [OpenHands blog](https://www.all-hands.dev/blog/sota-on-swe-bench-verified-with-inference-time-scaling-and-critic-model)
- **Agent-E.** Agent-E credits "change observation" (telling the agent what changed after each action) and DOM distillation/denoising as key design principles. It beat prior text and multimodal agents on WebVoyager "in most categories by 10-30%" (abstract only; per-category numbers not retrieved). — [Agent-E arXiv 2407.13032](https://arxiv.org/abs/2407.13032)
- **WebArena.** The best GPT-4 agent reached 14.41% end-to-end success against 78.24% for humans. The authors attribute the gap to missing "active exploration and failure recovery" (figures seen via search snippet of the paper). — [WebArena arXiv 2307.13854](https://arxiv.org/abs/2307.13854v4)
- **Invariant Labs trace analysis.** Analysis of a leading web agent's traces found many failures "easy to fix and avoidable", worth up to 16% on WebArena. This is a vendor blog (moderately reliable). — [Invariant Labs](https://invariantlabs.ai/blog/what-we-learned-from-analyzing-web-agents)

### Inferences
- **What to replace "24 steps × 5 chunks" with:**
  - a generous hard ceiling plus wall-clock and cost budgets;
  - a 75% "wrap up and save partial results" warning;
  - a forced done-only final step;
  - a soft loop detector (normalized action hashes over a ~20-step window, plus a page fingerprint of URL, element count and text hash) that injects escalating nudges;
  - a replan nudge after 3 consecutive failures;
  - a hard stop or human escalation only when the nudges fail. For example, an OpenHands-style rule: same action, same error, 3 times, nudge once, then stop. Another option is an "impossible_task / captcha / login needed" verdict that routes to the human.
- **Menu loops specifically.**
  - browser-use hashes clicks by element index. If the in-house listing re-numbers elements on each page, a loop on "the same menu" may escape index-based hashing. Hashing click by (role, visible text) plus page fingerprint would be more robust.
  - Stagnation detection (DOM unchanged after an action) catches menus that open and close without effect.
  - Anthropic's keyboard-shortcut advice is a cheap alternative action for menus.
- **Completion checks.** Self-assessed completion should be checked against an external artifact: a plan/todo with statuses, or a feature-list-style checklist written at the start. An end-of-run judge (cheap model plus last screenshots) can tell "done", "failed, retry" and "impossible, ask human" apart.

### Gaps
- No primary sources were retrieved for Devin/Cognition, SeeAct, WebVoyager's own agent loop, or AgentQ stopping logic. AgentQ (MCTS plus self-critique plus DPO) and WebVoyager (GPT-4V judge for evaluation) are known from the literature but were not re-verified this session.
- No published A/B numbers were found for loop-detector nudges in browser-use or OpenHands. The thresholds are engineering defaults, not measured optima.
- No quantitative study was found comparing fixed step caps against adaptive budgets for web agents.

## 2. Context management over long trajectories

### Takeaway
The consensus techniques are:
1. Keep a plan/todo that is rewritten so it sits at the end of context ("recitation").
2. Offload to files or notes, with restorable compression: drop page content but keep its URL or path.
3. Clear old tool results and screenshots, in batches so the prompt cache survives.
4. Summarize ("compact") periodically, with a summarizer prompt that refuses to infer completion.

Simple observation masking measurably matches LLM summarization at about half the cost.

### Cited Findings
**Manus**
- Agent tasks average ~50 tool calls, and the input-to-output token ratio is ~100:1. KV-cache hit rate is "the single most important metric for a production-stage AI agent", because cached Sonnet input costs $0.30/MTok against $3/MTok uncached. — [Manus – Context Engineering lessons](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- Cache hygiene:
  - keep the prompt prefix stable (no timestamp at the top);
  - make context append-only;
  - serialize JSON deterministically.
  - Source: [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- **todo.md recitation.** Manus rewrites a todo.md file through the task, which "pushes the global plan into the end of the context" and counters lost-in-the-middle drift and goal misalignment. — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- **File system as external memory.** Compression must be restorable: page content can be dropped if its URL is kept, and documents can be dropped if their sandbox path remains. — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- **Keep errors in context.** Failed actions and stack traces stay visible, because "seeing failures shifts the model's beliefs away from repeating similar mistakes." — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- **Avoid few-shot ruts.** Repetitive contexts make the agent mimic its own past pattern; in a 20-resume batch it drifted. The fix is controlled variation in serialization and phrasing. — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- **Mask tools instead of removing them.** Manus masks tool logits by state instead of adding or removing tools mid-run, since tool definitions sit near the context front and changing them breaks the cache and confuses the model. Tool names share prefixes (`browser_`, `shell_`) so whole groups can be constrained. — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)

**Anthropic context engineering**
- Anthropic names four techniques:
  - **compaction:** summarize and restart, keeping decisions, unresolved bugs and details;
  - **tool-result clearing:** "one of the safest, lightest-touch forms of compaction";
  - **structured note-taking:** NOTES.md or a todo list outside context;
  - **sub-agents:** they return condensed summaries of "often 1,000-2,000 tokens" from tens of thousands of tokens of exploration.
  - Source: [Anthropic – Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Context rot.** Recall degrades as context grows, "a performance gradient rather than a hard cliff". — [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Pokémon example.** Claude kept notes across thousands of steps (e.g. "Pikachu has gained 8 levels toward the target of 10") and built maps and achievement records without being prompted on memory structure. This is qualitative. — [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

**Screenshot management (Claude computer use docs)**
- Each screenshot costs ~1,000–1,800 input tokens. Above 20 images in a request, stricter per-image size limits apply. — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- The suggested default is "keep the last three screenshots and prune every 25 turns". Pruning in batches, not every turn, keeps the cache prefix stable. — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)

**browser-use compaction (on by default)**
- **When it runs.** Compaction happens every `compact_every_n_steps=25` steps, but only if history exceeds `trigger_char_count` (default 40,000 chars, ~10k tokens). — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)
- **What it keeps.** The last 6 items stay verbatim, and the summary is capped at 6,000 chars. A separate cheaper `compaction_llm` can be supplied. — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)
- **Summarizer prompt** — [browser-use message_manager/service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/message_manager/service.py):
  - "Capture task requirements, key facts, decisions, partial progress, errors, and next steps. Preserve important entities, values, URLs, and file paths."
  - "CRITICAL: Only mark a step as completed if you see explicit success confirmation in the history. If a step was started but not explicitly confirmed complete, mark it as 'IN-PROGRESS'. Never infer completion from context."
- **Other memory controls.** `max_history_items` limits how many recent steps stay in memory. Each `ActionResult` also has a `long_term_memory` string that is kept in history, while bulky `extracted_content` is not. — [browser-use docs](https://docs.browser-use.com/customize/agent/all-parameters), [views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)

**Measured: "The Complexity Trap" (SWE-agent on SWE-bench Verified, 5 model configurations)**
- Simple observation masking "halves cost relative to the raw agent" and matches, sometimes slightly exceeds, the solve rate of LLM summarization. A hybrid cuts cost a further 7% against masking and 11% against summarization. — [arXiv 2508.21433](https://arxiv.org/abs/2508.21433)
- OpenHands' `LLMSummarizingCondenser` is described as giving "~2x cost reduction". This is a secondary claim from a third-party GitHub issue, not an OpenHands source. — [hermes-agent issue #477](https://github.com/NousResearch/hermes-agent/issues/477)

### Inferences
- For a text page listing that occasionally uses vision:
  - keep the full current page listing only for the latest step;
  - replace older listings with a one-line "URL + what I did + result" (observation masking);
  - keep at most the last 2–3 screenshots;
  - keep a plan/todo with statuses that is re-emitted near the end of every prompt;
  - write findings or partial results to a scratch file or notes so a chunk boundary or compaction cannot lose them.
- Use a compaction prompt that forbids inferring completion (browser-use's wording). This directly targets "re-doing the same menu" after a summary drops the fact that it was already tried. List the approaches already tried and failed explicitly.
- For DeepSeek, prefix caching also makes a stable, append-only prompt worthwhile (see section 4 inferences).

### Gaps
- No web-agent-specific (WebArena/Mind2Web) measurement was found that isolates the effect of todo recitation or notes files. The evidence is qualitative (Manus, Anthropic) or from SWE tasks (Complexity Trap).
- The exact solve-rate numbers in the Complexity Trap paper were not retrieved (abstract only).

## 3. Waiting on slow jobs in a UI

### Takeaway
Production action spaces include a bounded `wait`: 30 s maximum in browser-use and 300 s maximum in Anthropic computer use. No primary source gives a measured polling or backoff policy for multi-minute UI jobs. For long waits, the pattern supported by the evidence is to stop the active loop, persist state, and re-observe later, rather than burning steps on wait/screenshot cycles.

### Cited Findings
- **browser-use.** Its `wait(seconds=3)` action is capped: `actual_seconds = min(max(seconds - 1, 0), 30)`. The wait is recorded in long-term memory as "Waited for N seconds". — [browser-use tools/service.py](https://github.com/browser-use/browser-use/blob/main/browser_use/tools/service.py)
- **Anthropic computer use.** The `wait` action takes "`duration`: seconds, up to 300 — Pause before the next action, for example, while an application loads." — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- **Third-party implementations.** One implementation returns a fresh screenshot automatically after a wait, so waiting and re-observing are a single action (third-party Go code, not Anthropic's spec). — [coder computeruse.go mirror](https://git.dragonfruit.dev/mirror/coder/raw/commit/75f5b60eb6a674220249e7da73158faef44db289/coderd/chatd/chattool/computeruse.go)
- **Steel MCP.** Its `wait` tool caps at 10 s. — [Glama listing of Steel MCP](https://glama.ai/mcp/servers/steel-dev/steel-mcp-server/tools/wait)
- **Wall-clock budgets.** The practitioner guidance to keep a separate wall-clock limit alongside step limits (secondary source) implies waiting should not consume the step budget. — [OneUptime blog](https://oneuptime.com/blog/post/2026-09-12-detect-tool-loops-dead-ends-production-agents/markdown)
- **Loop detector caveat.** browser-use's loop detector would count repeated identical `wait` calls, which hash the same, and the page-stagnation counter would fire after 5 unchanged pages. In browser-use a polling loop therefore gets "try a different approach" nudges, which is a false positive for legitimate waiting. — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)

### Inferences
- **Make "waiting for a job" a first-class state, not repeated steps.**
  - The agent declares what it is waiting for: a completion signal such as text, a URL or a download, plus an expected duration.
  - The harness re-observes on a backoff schedule (for example 5 s, 10 s, 20 s, 40 s, then capped at 60–120 s) without LLM calls, using a cheap DOM/text check for the completion signal.
  - It only wakes the LLM when the signal appears, the page changes materially, or a deadline passes.
  - For multi-minute jobs, persist state and schedule a later wake-up instead of holding the browser loop. The orgbots runtime already has a wakes module that could carry this.
- Exclude declared waits from loop and stagnation detectors and from step budgets. Count them against wall-clock time instead.

### Gaps
- No primary or academic source was found that measures polling cadence or backoff strategies for UI agents waiting on long jobs. The recommendations above are engineering inference.
- No Manus or Devin documentation was retrieved on how they handle long-running external jobs (e.g. Devin's sleep/wait behaviour).

## 4. Malformed structured output (JSON drift, XML-like tags), especially DeepSeek

### Takeaway
For DeepSeek, three fixes have the strongest support:
1. Use native tool calls with **strict mode** (beta endpoint), which validates against a JSON schema server-side and makes the model "strictly adhere" to it.
2. If you use `json_object` mode, put the word "json" plus an example in the prompt and set adequate `max_tokens`. Handle the documented "empty content" failure with a retry.
3. Add a **client-side fallback parser for DSML/XML-like tool-call markup** leaking into `content`. This is a known, widely reported DeepSeek V3.2/V4 behaviour.

Fallback models are a standard production pattern (browser-use `fallback_llm`).

### Cited Findings
**DeepSeek official docs**
- **JSON output mode.** Set `response_format={'type':'json_object'}`. Two prompt requirements apply: include the word "json" in the system or user prompt, and give an example of the desired JSON format. Also "Set the max_tokens parameter reasonably to prevent the JSON string from being truncated midway." — [DeepSeek JSON Output guide](https://api-docs.deepseek.com/guides/json_mode)
- **Known issue.** "The API may occasionally return empty content"; DeepSeek suggests modifying the prompt. — [DeepSeek JSON Output guide](https://api-docs.deepseek.com/guides/json_mode)
- **Strict mode (beta) for tool calls** — [DeepSeek Tool Calls guide](https://api-docs.deepseek.com/guides/tool_calls):
  - Enabling it: use `base_url="https://api.deepseek.com/beta"` and set `"strict": true` on every function. The server validates the schema and errors on unsupported types.
  - It works in both thinking and non-thinking mode.
  - Supported types: object, string, number, integer, boolean, array, enum, anyOf, plus `$ref`/`$def`.
  - Unsupported keywords: string `minLength`/`maxLength` and array `minItems`/`maxItems`.
  - Schema requirements: every property must be listed in `required`, and `additionalProperties: false` on every object.
- **Silent no-op.** Setting `strict` while keeping the default (non-beta) base URL means validation is not actually active (community guide, not official). — [horadecodar.com.br](https://horadecodar.com.br/?p=41360)
- **Current models.** The function-calling page lists models `deepseek-flash` and `deepseek-v4-pro`, an Anthropic-compatible base URL `https://api.deepseek.com/anthropic`, and `thinking`/`reasoning_effort` parameters. — [DeepSeek function calling guide](https://api-docs.deepseek.com/guides/function_calling)

**DSML leakage (XML-like tool calls in `content`)**
- DeepSeek V3.2 and V4 use an internal XML-like "DSML" tool-call grammar (`invoke`/`parameter` elements). V4 wraps calls in `<|DSML|tool_calls>` instead of V3.2's `<|DSML|function_calls>`. — [vLLM DeepSeek V4 tool parser docs](https://docs.vllm.ai/en/v0.21.0/api/vllm/tool_parsers/deepseekv4_tool_parser/)
- Users report the whole assistant message coming back as plain text in `message.content` with DSML tool-call markup and no structured `tool_calls`. — [HF DeepSeek-V4-Pro discussion #209](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/discussions/209); [LangChain forum](https://forum.langchain.com/t/the-output-content-has-been-corrupted/3551)
- **Fixes in the wild:**
  - server-side DeepSeek tool parsers in vLLM/SGLang (when self-hosting);
  - a client-side fallback: when there are no `tool_calls` and `finish_reason == "stop"`, scan `content` for DSML invoke blocks and convert them;
  - temporarily switching models.
  - Sources: community reports, no official DeepSeek statement found. [vLLM docs](https://docs.vllm.ai/en/v0.21.0/api/vllm/tool_parsers/deepseekv4_tool_parser/); [LangChain forum](https://forum.langchain.com/t/the-output-content-has-been-corrupted/3551)
- **Related gotcha.** DeepSeek reasoning mode returns HTTP 400 unless `reasoning_content` is included in assistant messages that precede tool calls. — [Zed commit fixing DeepSeek Reasoner tool calls](https://git.secluded.site/zed/commit/5dd8561b06c8af4ee46f3aa8bcf839f208b8c7bf)

**Generic patterns**
- browser-use defines a typed `AgentOutput` schema (thinking, evaluation, memory, next goal, actions). `max_failures=5` retries steps with errors, and `fallback_llm` takes over when the primary fails. — [browser-use docs](https://docs.browser-use.com/customize/agent/all-parameters)
- browser-use also offers a `flash_mode` that "skips evaluation, next goal and thinking and only uses memory". This is a smaller schema, which is useful for weaker or cheaper models. — [browser-use docs](https://docs.browser-use.com/customize/agent/all-parameters)
- Manus constrains actions with logit masking and response prefill. With the Hermes format:
  - "auto" prefills only the assistant header;
  - "required" prefills up to the tool-call token;
  - "specified" prefills up to the start of the function name.
  - This is a constrained-decoding approach available when self-hosting. — [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)

### Inferences
- **The XML-like drift is most likely DSML leakage.** The in-house symptom ("models switching to XML-like tags") is very likely this, especially on long prompts. A robust pipeline would:
  1. use native tool calls with strict mode on `/beta`, where schemas allow;
  2. parse `tool_calls` first, then fall back to DSML extraction from `content`, then to lenient JSON extraction and repair (strip fences, take the first balanced `{…}`, json-repair);
  3. on failure, re-ask with the parse error appended and a short schema reminder at the end of the prompt;
  4. after 2 failures, switch to a fallback model for that step.
- **Keep the structured payload small.** Large free-text "thinking" fields inside the JSON raise the drift risk. Prefer native reasoning (`reasoning_content`) plus a minimal action schema.

### Gaps
- No quantitative DeepSeek-specific measurement was found of JSON-mode or strict-mode failure rates, or of how drift scales with prompt length.
- The claim that end-of-prompt schema reminders reduce drift is common practitioner advice. No primary measurement was retrieved this session.

## 5. Procedural memory and skill learning

### Takeaway
Reusing induced workflows or skills is one of the best-measured wins for web agents:
- AWM: +51.1% relative success on WebArena and +24.6% on Mind2Web, with fewer steps;
- SkillWeaver: +31.8% relative on WebArena and +39.8% on real sites, and skills from strong agents lift weak agents by up to 54.3%;
- Agent KB: up to +6–18.7 pp on GAIA depending on version and metric.

Site-specific how-tos saved from successful runs and replayed as prompt context (AWM-style) are the low-cost variant.

### Cited Findings
- **Agent Workflow Memory (AWM)** — [AWM arXiv 2409.07429](https://arxiv.org/abs/2409.07429)
  - It induces reusable "workflows" (routines) from past trajectories, either offline from training examples or online from test queries as they arrive, and gives them to the agent as guidance.
  - Results: 24.6% relative success improvement on Mind2Web and 51.1% on WebArena, "along with fewer steps".
  - Online AWM beats baselines by 8.9–14.0 absolute points as train-test gaps widen (cross-task, cross-website, cross-domain).
- **SkillWeaver** — [SkillWeaver arXiv 2504.07079](https://arxiv.org/abs/2504.07079)
  - On a new website the agent proposes candidate skills, practices them, and "distills practice experiences into robust APIs" in a plug-and-play library. Repeated exploration keeps refining it ("honing").
  - Results: 31.8% relative improvement on WebArena and 39.8% on real-world sites.
  - APIs synthesized by strong agents improved weaker agents by up to 54.3% on WebArena.
- **ExpeL.** It gathers experiences on training tasks, extracts natural-language insights without parameter updates, and recalls insights and past trajectories at inference. The abstract reports "a consistent enhancement in its performance as it accumulates experiences" (benchmark numbers not retrieved). — [ExpeL arXiv 2308.10144](https://arxiv.org/abs/2308.10144)
- **Agent KB** — [Agent KB arXiv 2507.06229](https://arxiv.org/abs/2507.06229v3); [v5 PDF](https://arxiv.org/pdf/2507.06229v5)
  - It is a shared experience knowledge base across agent frameworks, with two-stage retrieval: workflows at planning time and targeted fixes at feedback time.
  - The headline numbers changed across versions:
    - v1: up to +16.28 pp on GAIA, and SWE-bench 41.33% → 53.33%;
    - v3: up to +6.06 pp on GAIA (pass@1);
    - v5: smolagents up to +18.7 pp (55.2% → 73.9%, pass@3 against baseline pass@1).
  - Cite the specific version.
- **Agent-E.** Agent-E reports "agentic self-improvement" and domain-specific primitive skills as design principles (abstract only). — [Agent-E arXiv 2407.13032](https://arxiv.org/abs/2407.13032)
- **Anthropic computer use docs.** "For repeatable tasks or UI interactions, include example screenshots and tool calls of successful outcomes in your prompt." This is a vendor-endorsed form of replaying site how-tos. — [Claude computer use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- **browser-use.** It has a `variable_detector.py` module in the agent package, consistent with parameterizing recorded trajectories for reuse. Its behaviour was not inspected in detail. — [browser-use repo](https://github.com/browser-use/browser-use/tree/main/browser_use/agent)

### Inferences
- **Suggested design.** After a run the judge marks successful, distill a site-scoped how-to: the goal pattern, the abstracted step sequence with variables, and pitfalls such as "menu X is under the kebab icon; the dropdown needs keyboard". Retrieve it by site and task similarity on the next run.
- **Failure lessons too.** Also store "what didn't work" (ExpeL-style insights), which directly addresses repeated menu loops.
- **Transfer to weaker models.** SkillWeaver's weak-agent transfer result suggests skills distilled by a stronger model (or a successful DeepSeek-pro run) can lift cheaper DeepSeek-flash runs.

### Gaps
- Voyager (Minecraft skill library) is the canonical precedent, but its abstract numbers were not re-verified this session.
- No measured results were found for browser-use's own memory or skill features.
- Absolute AWM/SkillWeaver success rates and step-count reductions were not retrieved (abstracts only).

## 6. Looking things up when stuck (docs or web search for how to use a UI)

### Takeaway
The evidence is thin and indirect. No study was found that measures a web agent searching documentation or tutorials at test time when stuck. Related evidence: WebArena deliberately embeds manuals the agents could use, but low scores were attributed to missing exploration and recovery. Giving agents API documentation plus browsing (hybrid agents) raised WebArena success to 38.9% (+24 points over browsing alone).

### Cited Findings
- **WebArena.** It includes knowledge resources such as user manuals as independent websites agents can reference. Yet the best GPT-4 agent scored 14.41% against 78.24% for humans, which the authors attribute to missing "active exploration and failure recovery". — [WebArena arXiv 2307.13854](https://arxiv.org/abs/2307.13854v4)
- **"Beyond Browsing" (CMU).** Hybrid agents that switch between API calls (with API documentation) and browsing "out-perform both others nearly uniformly across tasks". They reached a 38.9% success rate on WebArena, more than 24.0 points above browsing alone. — [arXiv 2410.16464](https://arxiv.org/html/2410.16464v3)
- **Agent KB.** Its "feedback" retrieval stage supplies targeted diagnostic fixes from other agents' experience when execution goes wrong. This is a lookup-when-stuck mechanism with the measured gains listed in section 5. — [Agent KB arXiv 2507.06229](https://arxiv.org/abs/2507.06229v3)
- **browser-use.** It ships a `search` action, and its loop detector normalizes search queries by sorted tokens. This means repeated near-identical searches are treated as loops, while genuinely different queries are not. — [browser-use views.py](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py)

### Inferences
- A "search the vendor's help docs for how to do X" action, triggered by the stall/replan nudge (not used freely), is a plausible, low-risk addition. Its value is likely highest for SaaS UIs with good help centers. The result should be distilled into the site how-to memory (section 5), so the lookup is paid once.
- Expect benefit mainly where the failure is "doesn't know where the feature is". Expect little where the failure is mechanical (dropdowns, timing).

### Gaps
- There is no direct quantitative evidence that test-time documentation or web lookup improves browser-agent success. AgentTrek and similar tutorial-based work use tutorials for training-data synthesis, not test-time lookup, and were not verified this session.
