"""The Anthropic provider.

M0 shipped an echo provider because what it was proving was the *accounting* around
a model call. M1 has to produce work a human accepts, so this is the first place
the runtime talks to a real model.

Four decisions, each with a consequence for the gate numbers.

**Structured output, not parsing.** Every M1 call has a pinned output schema, so
the request carries `output_config.format` and the response comes back conforming
or not at all. Parsing JSON out of prose would make "the schema failed" ambiguous
between "the agent produced the wrong thing" and "the agent produced the right
thing and we mis-read it" — and §10's first diagnostic question is exactly that
distinction.

**Adaptive thinking, effort as the dial.** Opus 5 thinks by default; `effort`
controls depth and spend. WORK runs at `high`, and everything overhead — planning,
evaluation, summarisation — runs at `medium`. That is a cost decision made once,
in the open, rather than a per-call temptation.

`EFFORT_BY_WORK_CLASS` remains that standing policy. A `ModelProfile` may override
it, and may turn thinking off outright, because those are the two dials that move
spend most and they belong beside the price they move — but a profile is authored
per actor and frozen into `spec_hash`, so an exception is a recorded decision and
not something a call site talks itself into.

**Streaming for long outputs.** A 16k-token draft on a non-streaming request can
outlive the HTTP timeout. `.stream()` with `get_final_message()` costs nothing and
removes a failure mode that would look like a flaky agent.

**Token counts come from the response, never from an estimate.** `usage` is what
the ledger records. An estimate that drifts from the invoice makes the single most
important number in §9 — cost per accepted outcome — a number nobody can defend.

The SDK client is constructed once per gateway and reused, so prompt caching has a
stable prefix to hit. This module is the only one in the tool layer that imports
`anthropic`, which the `gateway-only` contract permits precisely because it lives
under `runtime.gateway`.

**The endpoint and the capability set are fields, not constants.** Other vendors
speak the Messages protocol at their own base URL — DeepSeek is the one wired here
(`deepseek_provider`) — and each supports a *subset* of it. Rather than a second
copy of this file drifting out of sync, the four things that actually vary are
declared as `ProviderCapabilities` and the request builder branches on them. The
alternative — a provider that sends `output_config.format` to an endpoint that
ignores it — is the worst failure available: the schema is silently unenforced and
the first symptom is a validation error blamed on the model.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from runtime.domain.enums import TrustLevel, WorkClass
from runtime.domain.errors import MissingCredentials, ProviderUnavailable
from runtime.domain.specs import ModelProfile
from runtime.gateway.models import ModelRequest, ModelResponse
from runtime.observability.logging import get_logger

log = get_logger("gateway.anthropic")

EFFORT_BY_WORK_CLASS: dict[WorkClass, str] = {
    WorkClass.WORK: "high",
    WorkClass.COORDINATION: "medium",
    WorkClass.EVALUATION: "medium",
    WorkClass.SUMMARIZATION: "low",
}
"""Effort per class. Stated as data because it is a spend decision, not a tuning
knob: §10's "good failure" is high cost with everything else fine, and this table
is the first thing to move when that happens."""

DEFAULT_EFFORT = "medium"

_AUTH_HELP = (
    "no Anthropic credential could be resolved. Set ANTHROPIC_API_KEY, or run "
    "`ant auth login` and leave the variable unset — the SDK reads the profile. "
    "Check with `ant auth status`."
)

STREAM_ABOVE_TOKENS = 8_000

MAX_PAUSE_CONTINUATIONS = 4
"""How many times a `pause_turn` may be resumed inside one `complete()`.

A server-side tool loop that runs long comes back with `stop_reason: "pause_turn"`
and a partial assistant turn, and the protocol says to send it back to continue.
Un-resumed, the caller receives the model's interstitial narration — *"I have a rich
set of sources, let me search for a couple more angles"* — instead of the answer, and
`call_structured` reports that as `not_json` twice and fails the run on a schema
error that has nothing to do with the schema.

Bounded rather than `while True` because each continuation is a billable request
carrying the whole conversation so far, so a model that paused in a loop would spend
the actor's ceiling on narration. Four is past what a five-search budget needs and far
short of a ceiling. Exhausting it returns the partial text, which fails validation and
routes into the ordinary schema-failure path — the same treatment `refusal` gets."""
"""Above this `max_tokens`, stream. Non-streaming requests with a large output cap
can exceed the SDK's HTTP timeout, and a timeout mid-draft looks exactly like a
flaky agent while being a client configuration problem."""

SCHEMA_INSTRUCTION = (
    "Return a single JSON object and nothing else — no prose before it, no code "
    "fence around it. It must validate against this JSON Schema:\n\n{schema}"
)
"""The fallback for an endpoint that cannot enforce a schema server-side.

Asking in prose is strictly worse than `output_config.format` and the docstring
above says why, so it is used *only* where the capability is absent. It is written
out here rather than inline so the two paths are visibly different things: one is a
constraint, the other is a request."""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What this endpoint actually honours, as opposed to accepts and ignores.

    Every field defaults to the Anthropic behaviour, so a capability nobody thought
    about stays correct for the provider this file was written for and a vendor
    subset has to be declared explicitly.
    """

    schema_format: bool = True
    """`output_config.format` enforces the schema. False means it is ignored, and
    the schema goes into the system prompt instead — see `SCHEMA_INSTRUCTION`."""
    prompt_caching: bool = True
    """`cache_control` on the system block does something. False means send a plain
    string: an ignored breakpoint would make the `cache_read_tokens` log line below
    read as a cache bug rather than as a capability that is not there."""
    adaptive_thinking: bool = True
    web_search_tool: str | None = None
    """The server-side search tool's `type`, or None where the endpoint has none.
    Server-side search runs *outside* the tool gateway, so the gateway only lets it
    through for an actor that already holds `web.search@1` — see
    `ModelGateway._server_tools`."""
    web_search_max_uses: int = 5


@dataclass
class AnthropicProvider:
    """Talks to the Messages API. One client, reused."""

    name: str = "anthropic"
    api_key: str | None = None
    base_url: str | None = None
    """None means the SDK's own default — api.anthropic.com. A vendor speaking the
    same protocol sets its endpoint here."""
    auth_style: str = "api_key"
    """`api_key` sends `x-api-key`; `bearer` sends `Authorization: Bearer`. Which one
    a compatible endpoint wants is a property of that vendor, not of the protocol."""
    env_var: str = "ANTHROPIC_API_KEY"
    auth_help: str = _AUTH_HELP
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    client: Any = field(default=None, repr=False)
    calls: int = 0
    _key_fingerprint: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            key = self.api_key or os.environ.get(self.env_var)
            # A bare client also resolves an `ant auth login` profile, so an unset
            # env var is not on its own a missing credential. Construction is
            # therefore attempted regardless and only a failure is fatal.
            try:
                self.client = self._build_client(key)
            except Exception as exc:  # pragma: no cover - depends on local auth state
                raise ProviderUnavailable(
                    f"could not construct a client for {self.name}; {self.auth_help}"
                ) from exc

    def _build_client(self, key: str | None) -> Any:
        kwargs: dict[str, Any] = {}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if key:
            kwargs["auth_token" if self.auth_style == "bearer" else "api_key"] = key
        # No key is not fatal, for two different reasons depending on the endpoint:
        # Anthropic's SDK also resolves an `ant auth login` profile, and a
        # third-party endpoint may be fed from the M2 credentials table on the first
        # call. Both surface as `MissingCredentials` at call time if nothing
        # resolves, which names the fix; refusing here would break the store path.
        return anthropic.AsyncAnthropic(**kwargs)

    def use_credential(self, secret: str, *, fingerprint: str) -> None:
        """Swap in a credential from the M2 store, rebuilding the client if it changed.

        M2 moves credentials out of the environment, and this is how that reaches the
        model provider. It is *not* a per-call fetch of the kind the tool gateway
        does — the client is reused across calls on purpose, because prompt caching is
        a prefix match against a stable connection and rebuilding per call would throw
        the cache away on every request. The `ModelGateway` fetches per call; this
        rebuilds only when the fingerprint moves, which is exactly when a rotation has
        happened and the old client must stop being used anyway.

        The fingerprint, not the secret, is what is compared and stored — so a heap
        dump of this object does not contain a second copy of the key.
        """
        if fingerprint == self._key_fingerprint:
            return
        try:
            self.client = self._build_client(secret)
        except Exception as exc:  # pragma: no cover - depends on the SDK
            raise ProviderUnavailable(f"could not rebuild the {self.name} client") from exc
        self._key_fingerprint = fingerprint
        log.info("provider.credential_applied", provider=self.name, fingerprint=fingerprint)

    async def complete(self, profile: ModelProfile, req: ModelRequest) -> ModelResponse:
        max_tokens = req.max_output_tokens or profile.max_output_tokens
        work_class = req.metadata.get("work_class")
        # The profile wins when it says something. It is authored per actor and per
        # work class and frozen into the spec, so an override here is a decision
        # someone recorded — the table remains the standing policy for everything
        # that has not been given an exception.
        effort = profile.effort or EFFORT_BY_WORK_CLASS.get(
            WorkClass(work_class) if work_class else None,  # type: ignore[arg-type]
            DEFAULT_EFFORT,
        )

        caps = self.capabilities
        params: dict[str, Any] = {
            "model": profile.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
            "output_config": {"effort": effort},
        }
        if caps.adaptive_thinking:
            # `disabled` explicitly, not by omission. Opus 5 thinks by default, so
            # leaving the field out on a `thinking: false` profile would silently do
            # the opposite of what the profile says — and the bill would be the only
            # place it showed up.
            params["thinking"] = {"type": "adaptive"} if profile.thinking else {"type": "disabled"}

        system = req.system
        schema = req.metadata.get("json_schema")
        if schema is not None:
            if caps.schema_format:
                params["output_config"]["format"] = {
                    "type": "json_schema",
                    "schema": schema,
                }
            else:
                # Appended to the system block, not the user turn: the user turn is
                # the part that varies per call, and putting a fixed instruction
                # there would move the cache boundary on every request for the
                # endpoints that do cache.
                instruction = SCHEMA_INSTRUCTION.format(schema=json.dumps(schema))
                system = f"{system}\n\n{instruction}" if system else instruction

        if system:
            if caps.prompt_caching:
                # A list with an explicit cache breakpoint: the system block is the
                # stable prefix across every call an actor makes, so it is the one
                # place caching pays for itself.
                params["system"] = [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ]
            else:
                params["system"] = system

        tools = self._server_tools(profile, req)
        if tools:
            params["tools"] = tools

        # Accumulated across the turn's continuations rather than read off the last
        # message: every round trip is billed, and a ledger that recorded only the
        # final one would under-count a searching call by most of its cost.
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        server_searches = 0
        pauses = 0

        message: Any = None
        while True:
            try:
                message = await self._send_classified(params, max_tokens)
            except anthropic.BadRequestError as exc:
                # Only reachable on a continuation — the first send has no prior
                # message to fall back to and re-raises. Resuming a turn means handing
                # back blocks the vendor produced, and not every one of them is
                # accepted on the way in: a `web_search_tool_result` carrying an error,
                # or a thinking block the endpoint wants signed, is rejected as a
                # malformed request. Continuing is an optimisation over the partial
                # answer already in hand, so it may not turn a call that would merely
                # have retried into one that fails outright.
                if message is None:
                    raise
                log.warning(
                    "model.continuation_refused",
                    model=profile.model,
                    continuations=pauses,
                    error=str(exc)[:200],
                )
                break

            self.calls += 1
            usage = message.usage
            input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            server_searches += _server_search_count(message)

            # Two ways a turn ends without an answer. `pause_turn` is the documented
            # one: a long server-tool loop, resumed by sending the turn back. The
            # second is a vendor that ends the turn *inside* the loop — DeepSeek
            # returns `end_turn` with a `web_search_tool_result` as its last block and
            # no trailing text, so the caller gets "" and a structured call fails on
            # `not_json` having never seen the model try. Both are the same situation
            # and take the same continuation; only the stop reason differs.
            unanswered = bool(tools) and not _text_of(message).strip()
            if message.stop_reason != "pause_turn" and not unanswered:
                break
            if pauses >= MAX_PAUSE_CONTINUATIONS:
                log.warning(
                    "model.pause_unresolved",
                    model=profile.model,
                    continuations=pauses,
                    server_searches=server_searches,
                    stop_reason=message.stop_reason,
                )
                break
            pauses += 1
            # The assistant turn goes back verbatim — its `server_tool_use` and
            # `web_search_tool_result` blocks are what the continuation resumes from,
            # so filtering it to text would restart the search rather than finish it.
            params["messages"] = [
                *params["messages"],
                {"role": "assistant", "content": message.content},
            ]

        text = _text_of(message)

        if message.stop_reason == "refusal":
            # Not an exception. A refusal is a real outcome that has a cost and
            # should be visible in the ledger like any other call; the caller sees
            # empty text and its schema validation fails, which routes it into the
            # ordinary schema-failure path with the spend already recorded.
            log.warning(
                "model.refusal",
                model=profile.model,
                category=getattr(message.stop_details, "category", None),
            )
        elif message.stop_reason == "max_tokens":
            log.warning("model.truncated", model=profile.model, max_tokens=max_tokens)

        log.debug(
            "model.usage",
            model=profile.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Zero across repeated calls means a silent cache invalidator in the
            # system prefix — the fixed template exists partly to keep this non-zero.
            cache_read_tokens=cache_read_tokens,
            # Non-zero means this call did work the tool gateway never saw. It is the
            # only trace of a server-side search, so it is logged unconditionally
            # rather than only when the capability is on.
            server_searches=server_searches,
            continuations=pauses,
        )
        return ModelResponse(
            text=text,
            provider=self.name,
            model=profile.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=_cost_cents(profile, input_tokens, output_tokens),
            trust=TrustLevel.UNTRUSTED,
        )

    async def _send_classified(self, params: dict[str, Any], max_tokens: int) -> Any:
        """`_send`, with the SDK's failure modes mapped onto the runtime's taxonomy.

        Split out from `complete` when the `pause_turn` loop arrived: the mapping has
        to apply to every round trip of a turn, not only the first, and duplicating a
        six-branch `except` inside a loop is how one of the branches drifts.
        """
        try:
            return await self._send(params, max_tokens)
        except anthropic.AuthenticationError as exc:
            raise MissingCredentials(self.auth_help) from exc
        except TypeError as exc:
            # The SDK defers credential resolution to the first request, and when
            # nothing resolves it raises a bare `TypeError`. Left alone that reaches
            # the executor as an unclassified failure, and "the run crashed" and
            # "nobody configured a key" become the same log line. It is *not*
            # `ProviderUnavailable`: that is a `TransientFault`, and retrying a
            # missing credential just spends the retries.
            if "authentication method" not in str(exc):
                raise
            raise MissingCredentials(self.auth_help) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise ProviderUnavailable(f"{self.name} {exc.status_code}: {exc.message}") from exc
            raise
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(f"{self.name} connection error: {exc}") from exc

    def _server_tools(self, profile: ModelProfile, req: ModelRequest) -> list[dict[str, Any]]:
        """Server-side tools to offer on this request, if any.

        Three conditions, all required, and they are three because they answer three
        different questions. *Can* it — `capabilities.web_search_tool`, a property of
        the endpoint. *May* it — `metadata["server_tools"]`, which the gateway fills
        only for an actor holding `web.search@1`. *Should* it — `profile.web_search`,
        authored per work class, so an actor that searches while doing WORK does not
        also search while compressing history.

        The middle one is the load-bearing one. A search the provider runs on its own
        server never reaches `ToolGateway`: no `effect_intents` row, no per-connection
        rate limit, no grant check. Left ungated it would hand every actor with a
        model profile a web search that the authority tables say it does not have —
        `content` holds `web.fetch@1` and deliberately not search — and the audit
        trail would show a model call where a tool call happened. Gating on the grant
        keeps the two answers the same; the *accounting* still differs, and that is
        recorded on the usage row rather than hidden.
        """
        tool_type = self.capabilities.web_search_tool
        if not tool_type or not profile.web_search:
            return []
        offered = req.metadata.get("server_tools") or ()
        if "web.search@1" not in offered:
            return []
        return [
            {
                "type": tool_type,
                "name": "web_search",
                "max_uses": self.capabilities.web_search_max_uses,
            }
        ]

    async def _send(self, params: dict[str, Any], max_tokens: int) -> Any:
        if max_tokens >= STREAM_ABOVE_TOKENS:
            async with self.client.messages.stream(**params) as stream:
                return await stream.get_final_message()
        return await self.client.messages.create(**params)


def _text_of(message: Any) -> str:
    """The answer: the run of text blocks at the end of the message.

    Thinking blocks are never part of the answer, and neither are the
    `server_tool_use` / `web_search_tool_result` blocks a server-side search leaves
    behind: the model's summary of what it found *is* text, and that is what the
    caller's schema is validated against. Filtering on `type == "text"` rather than
    excluding known non-answer types means a block type nobody has seen yet is
    dropped rather than concatenated into the deliverable.

    **Only the trailing run, though.** A model that searches several times narrates
    between rounds — *"I have good material, let me check one more angle"* — and each
    of those is a text block too. Concatenating all of them prefixes the answer with
    prose, and for a structured call that is fatal in a way that reads as the model's
    fault: `call_structured` reports `not_json`, spends its one retry, and fails the
    task on a schema error when the JSON it wanted was sitting at the end of the
    string all along. Text before a tool call is what the model said on the way to the
    answer; text after the last one is the answer.

    A message with no tool or thinking blocks has no such boundary, so everything is
    the answer and this is the plain concatenation it always was.
    """
    blocks = list(message.content)
    last_non_text = max(
        (i for i, b in enumerate(blocks) if getattr(b, "type", None) != "text"),
        default=-1,
    )
    return "".join(
        block.text
        for block in blocks[last_non_text + 1 :]
        if getattr(block, "type", None) == "text"
    )


def _server_search_count(message: Any) -> int:
    return sum(
        1 for block in message.content if getattr(block, "type", None) == "web_search_tool_result"
    )


def _cost_cents(profile: ModelProfile, input_tokens: int, output_tokens: int) -> int:
    """Cents, from the response's own token counts.

    Integer arithmetic against a per-Mtok rate, so a cheap call rounds to zero
    rather than to a fraction the ledger cannot store. That under-counts by at most
    a cent per call, which is the right direction to be wrong in — the alternative
    inflates cheap calls and makes the coordination ratio look worse than it is,
    which would be a lie in our own favour when the ratio is the thing under test.
    """
    return (
        input_tokens * profile.input_cents_per_mtok + output_tokens * profile.output_cents_per_mtok
    ) // 1_000_000
