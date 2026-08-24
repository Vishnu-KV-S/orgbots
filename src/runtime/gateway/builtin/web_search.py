"""`web.search@1` — the first tool with a price on it.

`replay_safe` and READ blast radius: a search mutates nothing, so re-executing it
after a crash costs a request and a cent. That is the honest declaration —
`check_entailment` would reject it if the tool also claimed to mutate state.

**It refuses rather than degrades.** With no `search_endpoint_url` configured the
tool raises. The tempting alternative — return an empty result list and let the
agent carry on — produces a `CompetitorReport` whose sources are invented, and that
is the worst failure available to this milestone: it looks like work, a fluent
model will get it past schema validation, and the only thing that would catch it is
the 20% human sample weeks later. A loud configuration error on day one is worth a
great deal more.

**It is priced.** The M0 retro noted that `estimate_tool_cents()` returned zero for
every tool and was therefore a seam rather than a number. This is the tool that
closes that: searches reserve real cents before firing, so the budget path is
exercised with a non-zero amount and `cost per accepted outcome` includes the cost
of looking things up.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.domain.errors import ProviderUnavailable, SpecError
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.settings import Settings

MAX_RESULTS = 10


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=400)
    max_results: int = Field(default=5, ge=1, le=MAX_RESULTS)


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str = ""


class WebSearchResult(BaseModel):
    query: str
    results: list[SearchHit]
    provider: str


def build(settings: Settings) -> tuple[ToolDef, Any]:
    async def web_search(ctx: ToolContext, args: Any) -> WebSearchResult:
        _ = ctx
        typed: WebSearchArgs = args
        endpoint = settings.search_endpoint_url
        if not endpoint:
            raise SpecError(
                "web.search@1 is not configured: set RUNTIME_SEARCH_ENDPOINT_URL. "
                "The tool refuses rather than returning an empty result set, because "
                "an agent handed zero sources writes a report with invented ones."
            )
        headers = {"accept": "application/json"}
        if settings.search_api_key:
            headers["authorization"] = f"Bearer {settings.search_api_key}"

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                endpoint,
                params={"q": typed.query, "count": typed.max_results},
                headers=headers,
            )
        if response.status_code >= 500:
            # Transient by taxonomy, so the gateway's retry policy applies. A 4xx is
            # a configuration or quota problem and retrying it just spends the quota
            # faster, so it falls through to the generic failure path.
            raise ProviderUnavailable(f"search endpoint returned {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        raw = payload.get("results") or payload.get("web", {}).get("results") or []
        hits = [
            SearchHit(
                title=str(item.get("title", ""))[:300],
                url=str(item.get("url", "")),
                snippet=str(item.get("snippet") or item.get("description") or "")[:1000],
            )
            for item in raw[: typed.max_results]
            if item.get("url")
        ]
        return WebSearchResult(query=typed.query, results=hits, provider="http")

    definition = ToolDef(
        name="web.search",
        version=1,
        args_model=WebSearchArgs,
        result_model=WebSearchResult,
        capabilities=EffectCapabilities(
            mutates_external_state=False,
            max_blast_radius=BlastRadius.READ,
        ),
        recovery_policy=RecoveryPolicy.REPLAY_SAFE,
        timeout_s=25.0,
    )
    return definition, web_search


def register(registry: ToolRegistry, settings: Settings) -> None:
    definition, fn = build(settings)
    registry.register(definition, fn)
