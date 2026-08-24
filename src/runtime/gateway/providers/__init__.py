"""Model providers.

Kept in a subpackage so `runtime.gateway.models` — which every actor imports
transitively — does not pull the Messages SDK into a process that only needs the
echo provider. The tests, the CLI and the migration path all care about that.

`build_providers()` is the one place a provider is chosen, and it is deliberately
not configurable per actor: an actor names a *profile*, the profile names a
provider string, and this maps that string to an implementation. An actor that
could name a client could name an unaudited one.

**Two names, and `anthropic` is not one of them.** `deepseek` and `fake` are what a
profile may resolve to. `AnthropicProvider` is still imported and still does the
work — it is the Messages client `DeepSeekProvider` subclasses, and DeepSeek's
server-side search is that class's pause-and-continue loop driving an Anthropic
server-tool type — but it is no longer registered under a name of its own, so no
profile can route a call to `api.anthropic.com`. Putting it back is one line here
plus its entry in `DEFAULT_PROVIDER_CREDENTIALS`; leaving it out is what makes
"which vendor sees our prompts" answerable from this file alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runtime.gateway.models import EchoProvider, Provider
from runtime.observability.logging import get_logger
from runtime.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover
    from runtime.settings import Settings


def build_providers(settings: Settings | None = None) -> dict[str, Provider]:
    """Every provider this process can reach.

    The client is constructed lazily and its failure is not fatal: a developer
    running the deterministic actor and the test suite has no reason to need
    credentials, and a hard failure here would make `analytics` — the one actor that
    must never call a model — undeployable without an API key.
    """
    resolved = settings or get_settings()
    providers: dict[str, Provider] = {"fake": EchoProvider()}

    if resolved.deepseek_enabled:
        try:
            # `ProviderCapabilities` comes from the Anthropic module because it
            # describes the Messages protocol rather than a vendor: every field
            # defaults to what that endpoint honours, and DeepSeek is expressed as
            # the subset it does not.
            from runtime.gateway.providers.anthropic_provider import ProviderCapabilities
            from runtime.gateway.providers.deepseek_provider import DeepSeekProvider

            providers["deepseek"] = DeepSeekProvider(
                api_key=resolved.deepseek_api_key,
                base_url=resolved.deepseek_base_url,
                capabilities=ProviderCapabilities(
                    schema_format=False,
                    prompt_caching=False,
                    # The one capability an operator turns on, and the only one that
                    # can reach the network outside the tool gateway. Absent unless
                    # asked for by name.
                    web_search_tool=(
                        resolved.deepseek_web_search_tool if resolved.deepseek_web_search else None
                    ),
                    web_search_max_uses=resolved.deepseek_web_search_max_uses,
                ),
            )
        except Exception as exc:  # pragma: no cover - depends on local credentials
            # Left absent rather than stubbed. `ModelGateway.complete` then raises
            # `ModelCallNotAllowed: provider 'deepseek' is not configured`, which names
            # the actual problem — a stub would produce plausible garbage and a week of
            # debugging prompts. Logged at warning so the absence is visible at startup
            # rather than at the first model call.
            get_logger("gateway.providers").warning(
                "provider.unavailable", provider="deepseek", error=str(exc)
            )
    return providers


__all__ = ["build_providers"]
