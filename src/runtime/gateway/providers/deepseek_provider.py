"""The DeepSeek provider.

DeepSeek publishes an Anthropic-compatible endpoint at
`https://api.deepseek.com/anthropic` that speaks the Messages protocol, so this is
a *configuration* of `AnthropicProvider` rather than a second client. That is the
whole reason there is no `openai` dependency in this project: the OpenAI-compatible
endpoint would have meant a second SDK, a second streaming implementation and a
second error taxonomy to keep aligned with the first, for a wire format that carries
less of what this runtime already depends on.

**What the endpoint does not honour**, from DeepSeek's own compatibility table, and
what each costs us:

- **`output_config.format`** — only `effort` is read. So the schema cannot be
  enforced server-side and goes into the system prompt instead
  (`capabilities.schema_format=False`). This is the one real regression: M1's
  structured-output contract says *"the schema is sent, not described"* precisely so
  that a validation failure is unambiguous, and on DeepSeek it is not. Expect a
  higher rate of `call_structured` retries, and read a schema failure here as
  possibly ours. It is the reason the recommended first placement is SUMMARIZATION,
  whose output is never the artifact.
- **`cache_control`** — ignored, so the system block goes as a plain string and
  `cache_read_tokens` will read zero. Sending the breakpoint anyway would make that
  zero look like a cache bug worth a morning of investigation.
- **`budget_tokens` on `thinking`** — ignored. `effort` still selects depth, which
  is the dial `EFFORT_BY_WORK_CLASS` was already turning.

**Web search is real and it is server-side.** DeepSeek runs the search itself and
returns `server_tool_use` / `web_search_tool_result` blocks; there is no client-side
tool call and therefore no `ToolGateway` involvement. That is a governance hole and
it is closed at two points rather than one: the capability is off unless
`RUNTIME_DEEPSEEK_WEB_SEARCH` is set, and the gateway offers the tool only to an
actor holding `web.search@1`. The extra summarisation tokens DeepSeek spends on the
retrieved pages arrive in `usage` like any others, so the ledger stays honest even
though the effect journal never sees the fetch.

**Auth is bearer, not `x-api-key`.** DeepSeek's own Claude Code instructions set
`ANTHROPIC_AUTH_TOKEN`, which is the SDK's `auth_token` parameter and an
`Authorization: Bearer` header.

Models are `deepseek-v4-flash` and `deepseek-v4-pro`; the older `deepseek-chat` and
`deepseek-reasoner` names were retired in July 2026. Which one an actor uses, and at
what price, is not decided here — it is a `ModelProfile`, authored in the agents
YAML (`runtime.org.agents_config`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.gateway.providers.anthropic_provider import (
    AnthropicProvider,
    ProviderCapabilities,
)

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"

DEFAULT_WEB_SEARCH_TOOL = "web_search_20250305"
"""The Anthropic server-tool type DeepSeek mirrors. Configurable rather than
hardcoded (`RUNTIME_DEEPSEEK_WEB_SEARCH_TOOL`) because it is a vendor-side string:
if DeepSeek versions it, that must be a settings change and not a release."""

AUTH_HELP = (
    "no DeepSeek credential could be resolved. Set DEEPSEEK_API_KEY (or "
    "RUNTIME_DEEPSEEK_API_KEY), or store one with `python -m runtime.cli credentials "
    "--put deepseek_api_key --provider deepseek`."
)


@dataclass
class DeepSeekProvider(AnthropicProvider):
    """`AnthropicProvider` pointed at DeepSeek, with the subset declared.

    Subclassed rather than constructed inline in `build_providers` so that the
    capability set travels with the provider it describes: a future
    `build_providers` that forgot one of these fields would produce a client that
    silently under-enforces, and the failure would surface as bad model output.
    """

    name: str = "deepseek"
    base_url: str | None = DEFAULT_BASE_URL
    auth_style: str = "bearer"
    env_var: str = "DEEPSEEK_API_KEY"
    auth_help: str = AUTH_HELP
    capabilities: ProviderCapabilities = field(
        default_factory=lambda: ProviderCapabilities(
            schema_format=False,
            prompt_caching=False,
            adaptive_thinking=True,
            web_search_tool=None,
        )
    )
