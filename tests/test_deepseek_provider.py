"""The DeepSeek provider, and the capability branches it exists to exercise.

No network and no database: the SDK client is replaced with a stub that records the
parameters it was handed. What is being tested is not that DeepSeek answers — it is
that the request we build for an endpoint with a *subset* of the Messages protocol
differs from the Anthropic one in exactly the four places it should, and nowhere
else. A provider that sends `output_config.format` to an endpoint that ignores it
produces unenforced schemas and blames the model, which is the failure this file is
here to prevent.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
import pytest

from runtime.domain.enums import TrustLevel, WorkClass
from runtime.domain.errors import MissingCredentials, ProviderUnavailable
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import ModelProfile
from runtime.gateway.models import SERVER_TOOL_GRANTS, ModelGateway, ModelRequest
from runtime.gateway.providers import build_providers
from runtime.gateway.providers.anthropic_provider import AnthropicProvider, ProviderCapabilities
from runtime.gateway.providers.deepseek_provider import DeepSeekProvider
from runtime.settings import Settings
from tests.conftest_m2 import make_authority, make_ctx

PROFILE = ModelProfile(
    provider="deepseek",
    model="deepseek-v4-flash",
    max_output_tokens=1_000,
    input_cents_per_mtok=22,
    output_cents_per_mtok=66,
)

SEARCHING_PROFILE = PROFILE.model_copy(update={"web_search": True})

SCHEMA = {"type": "object", "properties": {"headline": {"type": "string"}}}


# --- a stub that records what it was asked for --------------------------------------


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Usage:
    input_tokens: int = 1_000_000
    output_tokens: int = 1_000_000
    cache_read_input_tokens: int = 0


@dataclass
class _Message:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"
    stop_details: Any = None


class _Messages:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.params: dict[str, Any] = {}

    async def create(self, **params: Any) -> Any:
        self.params = params
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _Client:
    def __init__(self, reply: Any) -> None:
        self.messages = _Messages(reply)


def _provider(reply: Any = None, **caps: Any) -> DeepSeekProvider:
    message = reply if reply is not None else _Message(content=[_Block("text", "ok")])
    return DeepSeekProvider(
        client=_Client(message),
        capabilities=ProviderCapabilities(schema_format=False, prompt_caching=False, **caps),
    )


def _request(**metadata: Any) -> ModelRequest:
    return ModelRequest(
        prompt="draft it",
        system="You are content.",
        metadata={"work_class": WorkClass.SUMMARIZATION.value, **metadata},
    )


# --- the capability subset ----------------------------------------------------------


async def test_schema_goes_into_the_prompt_when_the_endpoint_cannot_enforce_it() -> None:
    """DeepSeek reads only `effort` from `output_config`. Sending `format` anyway
    would leave the schema silently unenforced — the exact ambiguity M1's structured
    output contract was written to remove."""
    provider = _provider()
    await provider.complete(PROFILE, _request(json_schema=SCHEMA))

    params = provider.client.messages.params
    assert "format" not in params["output_config"], "format is ignored by this endpoint"
    assert params["output_config"]["effort"] == "low", "SUMMARIZATION's effort still applies"
    assert json.dumps(SCHEMA) in params["system"], "so the schema must be described instead"
    assert "You are content." in params["system"], "and the caller's system prompt survives"


async def test_the_schema_instruction_rides_on_the_system_block_not_the_user_turn() -> None:
    """The user turn varies per call. A fixed instruction there would move the cache
    boundary on every request for the endpoints that do cache."""
    provider = _provider()
    await provider.complete(PROFILE, _request(json_schema=SCHEMA))

    params = provider.client.messages.params
    assert params["messages"] == [{"role": "user", "content": "draft it"}]


async def test_no_cache_breakpoint_where_cache_control_is_ignored() -> None:
    """A breakpoint the endpoint drops makes `cache_read_tokens=0` read as a cache
    bug rather than as a capability that is not there."""
    provider = _provider()
    await provider.complete(PROFILE, _request())

    assert provider.client.messages.params["system"] == "You are content."


async def test_anthropic_still_sends_the_breakpoint_and_the_schema_format() -> None:
    """The regression guard on the refactor: parameterising the provider must not
    have changed the base behaviour it was written for.

    `anthropic` is no longer a registered provider — nothing routes a call there — but
    this class is still the Messages client `DeepSeekProvider` subclasses, and every
    `ProviderCapabilities` field defaults to what is asserted here. So this is not a
    test of a dead vendor: it is the reference the DeepSeek subset is defined against,
    and a silent drift here would move that subset without touching its file.
    """
    provider = AnthropicProvider(client=_Client(_Message(content=[_Block("text", "ok")])))
    await provider.complete(
        ModelProfile(provider="anthropic", model="claude-opus-5"),
        _request(json_schema=SCHEMA),
    )

    params = provider.client.messages.params
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["thinking"] == {"type": "adaptive"}
    assert json.dumps(SCHEMA) not in str(params["system"]), "constrained, not described"


# --- thinking and effort ------------------------------------------------------------


async def test_thinking_is_disabled_explicitly_not_by_omission() -> None:
    """An adaptive-thinking endpoint thinks by default. Dropping the field on a
    `thinking: false` profile would do the opposite of what the profile says, and the
    bill would be the only place it showed up."""
    provider = _provider()
    await provider.complete(PROFILE.model_copy(update={"thinking": False}), _request())

    assert provider.client.messages.params["thinking"] == {"type": "disabled"}


async def test_thinking_defaults_to_on() -> None:
    provider = _provider()
    await provider.complete(PROFILE, _request())

    assert provider.client.messages.params["thinking"] == {"type": "adaptive"}


async def test_the_profile_overrides_the_work_class_effort_table() -> None:
    provider = _provider()
    await provider.complete(PROFILE.model_copy(update={"effort": "max"}), _request())

    assert provider.client.messages.params["output_config"]["effort"] == "max"


async def test_the_effort_table_still_applies_when_the_profile_is_silent() -> None:
    """The table is the standing policy; the profile field is for the exception.
    A profile that says nothing must not quietly become `medium` everywhere."""
    provider = _provider()
    await provider.complete(PROFILE, _request())

    assert PROFILE.effort is None
    assert provider.client.messages.params["output_config"]["effort"] == "low", "SUMMARIZATION"


# --- server-side web search ---------------------------------------------------------


async def test_web_search_is_absent_unless_the_capability_is_configured() -> None:
    """Each of the three gates is tested with the other two open, so a test that
    passes because a different gate happened to be shut is not possible here."""
    provider = _provider(web_search_tool=None)
    await provider.complete(SEARCHING_PROFILE, _request(server_tools=("web.search@1",)))

    assert "tools" not in provider.client.messages.params


async def test_web_search_is_absent_unless_the_actor_holds_the_grant() -> None:
    """The governance half. DeepSeek runs this search on its own server: no journal
    row, no per-connection limit, no approval. An actor that could not call
    `web.search@1` through the front door must not get it through this door."""
    provider = _provider(web_search_tool="web_search_20250305")
    await provider.complete(SEARCHING_PROFILE, _request(server_tools=("web.fetch@1",)))

    assert "tools" not in provider.client.messages.params


async def test_web_search_is_absent_unless_the_profile_asks_for_it() -> None:
    """The third key: capability and grant say it *can* and *may*, the profile says
    whether it *should*. `research` searches while doing WORK and not while
    compressing history, and that is one profile apart."""
    provider = _provider(web_search_tool="web_search_20250305")
    await provider.complete(PROFILE, _request(server_tools=("web.search@1",)))

    assert PROFILE.web_search is False
    assert "tools" not in provider.client.messages.params


async def test_web_search_is_offered_when_all_three_agree() -> None:
    provider = _provider(web_search_tool="web_search_20250305", web_search_max_uses=3)
    await provider.complete(SEARCHING_PROFILE, _request(server_tools=("web.search@1",)))

    assert provider.client.messages.params["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    ]


async def test_search_result_blocks_are_not_part_of_the_answer() -> None:
    """`web_search_tool_result` blocks carry the raw pages. The model's summary of
    them is the text, and the text is what the caller's schema validates against."""
    reply = _Message(
        content=[
            _Block("server_tool_use"),
            _Block("web_search_tool_result"),
            _Block("text", "three competitors ship agent runtimes"),
        ]
    )
    provider = _provider(reply, web_search_tool="web_search_20250305")
    response = await provider.complete(SEARCHING_PROFILE, _request(server_tools=("web.search@1",)))

    assert response.text == "three competitors ship agent runtimes"


# --- accounting and failure ---------------------------------------------------------


async def test_cost_and_tokens_come_from_the_response() -> None:
    provider = _provider()
    response = await provider.complete(PROFILE, _request())

    assert (response.input_tokens, response.output_tokens) == (1_000_000, 1_000_000)
    assert response.cost_cents == 22 + 66, "one Mtok each way at the profile's rate"
    assert response.provider == "deepseek"
    assert response.trust is TrustLevel.UNTRUSTED


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, ProviderUnavailable), (503, ProviderUnavailable), (401, MissingCredentials)],
)
async def test_status_errors_are_classified(status: int, expected: type[Exception]) -> None:
    """`ProviderUnavailable` is a `TransientFault` and gets retried; a missing
    credential must not be, or the retries are spent on a fault no retry can fix."""
    request = httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"message": "no"}})
    error: Exception = (
        anthropic.AuthenticationError("unauthorized", response=response, body=None)
        if status == 401
        else anthropic.APIStatusError("upstream", response=response, body=None)
    )

    with pytest.raises(expected) as caught:
        await _provider(error).complete(PROFILE, _request())

    if expected is MissingCredentials:
        assert "DEEPSEEK_API_KEY" in str(caught.value), "the error has to name the fix"
    else:
        assert "deepseek" in str(caught.value), "not 'anthropic 429' from a DeepSeek call"


# --- wiring -------------------------------------------------------------------------
#
# Every field these tests depend on is passed explicitly. `Settings` reads `.env`, so a
# bare `Settings()` here would assert against whatever the developer happens to have
# switched on locally — which is a test that passes on one machine and fails on the
# next, for a reason that has nothing to do with the code.


def _settings(**overrides: Any) -> Settings:
    return Settings(
        **{
            "deepseek_enabled": True,
            "deepseek_api_key": "k",
            "deepseek_web_search": False,
            "deepseek_web_search_tool": "web_search_20250305",
            **overrides,
        }
    )


def test_the_provider_is_absent_until_it_is_enabled() -> None:
    """A registered-but-unreachable provider fails at the first WORK call with a 401.
    An absent one fails at admission with a message naming the provider."""
    assert "deepseek" not in build_providers(_settings(deepseek_enabled=False))
    assert "deepseek" in build_providers(_settings())


def test_deepseek_authenticates_with_a_bearer_token() -> None:
    """DeepSeek's own instructions set `ANTHROPIC_AUTH_TOKEN`, which is
    `Authorization: Bearer` — not the `x-api-key` header Anthropic uses."""
    provider = build_providers(_settings(deepseek_api_key="sk-ds"))["deepseek"]

    assert provider.auth_style == "bearer"
    assert provider.client.auth_token == "sk-ds"
    assert str(provider.client.base_url).startswith("https://api.deepseek.com/anthropic")


def test_web_search_capability_follows_the_setting() -> None:
    off = build_providers(_settings())["deepseek"]
    on = build_providers(_settings(deepseek_web_search=True))["deepseek"]

    assert off.capabilities.web_search_tool is None
    assert on.capabilities.web_search_tool == "web_search_20250305"


# --- the gateway's half of the search gate ------------------------------------------


def _org() -> OrganizationId:
    return OrganizationId(uuid.uuid4())


def test_gateway_offers_a_server_tool_only_on_a_live_grant() -> None:
    org = _org()
    granted = make_ctx(
        org,
        allowed_tools=frozenset({"web.search@1"}),
        authority=make_authority("research", tools=frozenset({"web.search@1"})),
    )
    assert ModelGateway._server_tools(granted) == ("web.search@1",)


def test_gateway_refuses_a_tool_the_spec_admitted_but_the_grant_revoked() -> None:
    """`allowed_tools` is what the actor was admitted with; `tool_grants` is what it
    may use now. The tool gateway checks both, so this door must too — otherwise the
    side door is the looser of the two, and the looser one is the stale one."""
    org = _org()
    revoked = make_ctx(
        org,
        allowed_tools=frozenset({"web.search@1"}),
        authority=make_authority("research", tools=frozenset()),
    )
    assert ModelGateway._server_tools(revoked) == ()


def test_only_research_asks_for_a_provider_side_search_by_default() -> None:
    """And only during WORK. A search while compressing history is spend with nothing
    to gain, and `content` has no grant for the request to pair with anyway."""
    from runtime.org.department import CONTENT, DEPARTMENT_SPECS, RESEARCH

    by_name = {spec.name: spec for spec in DEPARTMENT_SPECS}
    research = by_name[RESEARCH].model_profiles

    assert research.for_work_class(WorkClass.WORK).web_search is True
    assert research.for_work_class(WorkClass.SUMMARIZATION).web_search is False
    assert by_name[CONTENT].model_profiles.for_work_class(WorkClass.WORK).web_search is False


def test_gateway_offers_nothing_to_an_actor_without_the_tool() -> None:
    """`content` holds `web.fetch@1` and deliberately not search."""
    org = _org()
    ctx = make_ctx(
        org,
        actor_name="content",
        allowed_tools=frozenset({"web.fetch@1"}),
        authority=make_authority("content", tools=frozenset({"web.fetch@1"})),
    )
    assert ModelGateway._server_tools(ctx) == ()
    assert "web.fetch@1" not in SERVER_TOOL_GRANTS, "fetch has no server-side equivalent"
