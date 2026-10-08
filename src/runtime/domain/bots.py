"""Bots — the values. Pure: no I/O, importable from a unit test with nothing running.

A bot is an actor plus a conversation. What lives here is everything about a bot that
can be decided without touching the world:

- `BotStep` — the one object the model produces per turn of the loop. Pinned as
  `BotStep@1` like every other model output, so "the model answered badly" and "we
  parsed it badly" stay distinguishable, and a malformed step gets one corrective
  retry through `call_structured` rather than a bespoke parser.
- `needs_approval` — the bot-level gate, as a function of the step, the element it
  touches and the person's rules. **Ask first wins**: an `ask` rule beats an `allow`
  rule for the same action, and both beat the model's own opinion of whether a step is
  sensitive — in the direction of asking, never of not asking.
- Deterministic message ids, so a replayed node re-writes the same rows.

The loop is a bounded one: a run is one turn of a conversation, capped at
`MAX_STEPS` actions. A task that needs more ends the turn with a progress report and
the person says "continue" — the same shape as a person checking in, and it keeps a
confused bot from spending a whole budget pool in one silent run.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.schemas import SCHEMAS

MAX_STEPS = 24
"""Browser actions per run. A turn that needs more reports progress and stops."""

MAX_HISTORY_MESSAGES = 24
"""Conversation messages (person and bot, not activity) carried into a run's input."""

MAX_MEMORY_CHARS = 4_000
"""What a bot may keep in its learned notes. Old notes fall off the front."""

BROWSER_ACTIONS = frozenset(
    {
        "navigate",
        "click",
        "type",
        "press",
        "select",
        "scroll",
        "hover",
        "back",
        "forward",
        "reload",
        "wait",
    }
)
"""Steps that become a `browser.act@1` call. Must match `runtime.computer.browser`."""

TURN_ENDING = frozenset({"reply", "ask_user"})
"""Steps that hand the conversation back to the person."""

ACTIONS_NEEDING_ELEMENT = frozenset({"click", "type", "select", "hover"})

StepAction = Literal[
    "navigate",
    "click",
    "type",
    "press",
    "select",
    "scroll",
    "hover",
    "back",
    "forward",
    "reload",
    "wait",
    "observe",
    "remember",
    "reply",
    "ask_user",
]


class BotStep(BaseModel):
    """One decision. Exactly one action per step, and a reason for it."""

    model_config = ConfigDict(extra="forbid")

    thought: str = Field(
        max_length=1_200,
        description="Brief reasoning: what you see, what you are trying to do, and why "
        "this action is the next one. Shown to the person as your activity.",
    )
    action: StepAction = Field(
        description="navigate/click/type/press/select/scroll/hover/back/forward/reload/"
        "wait drive the browser. observe re-reads the page. remember saves a note to "
        "your long-term memory and continues. reply ends your turn with a message to "
        "the person. ask_user ends your turn with a question you need answered."
    )
    element: int | None = Field(
        default=None,
        description="The [number] of the element, from the LATEST page listing only. "
        "Required for click, type, select and hover.",
    )
    url: str | None = Field(default=None, description="For navigate.")
    text: str | None = Field(
        default=None,
        description="The text to type (type), the message (reply / ask_user), or the "
        "note to save (remember).",
    )
    key: str | None = Field(default=None, description="For press, e.g. Enter, Tab, Escape.")
    option: str | None = Field(default=None, description="For select: the option's label.")
    direction: Literal["up", "down"] | None = Field(default=None, description="For scroll.")
    seconds: float | None = Field(default=None, ge=0, le=10, description="For wait.")
    submit: bool = Field(default=False, description="For type: press Enter afterwards.")
    sensitive: bool = Field(
        default=False,
        description="True if this action has a real-world consequence that is hard to "
        "undo: submitting an order or payment, sending a message or email, posting "
        "publicly, deleting something, accepting terms, changing account settings, or "
        "entering a password or other secret.",
    )

    @model_validator(mode="after")
    def _check_fields(self) -> BotStep:
        if self.action in ACTIONS_NEEDING_ELEMENT and self.element is None:
            raise ValueError(f"{self.action} needs `element` — a number from the latest listing")
        if self.action == "navigate" and not (self.url or "").strip():
            raise ValueError("navigate needs `url`")
        if self.action == "type" and self.text is None:
            raise ValueError("type needs `text`")
        if self.action == "press" and not (self.key or "").strip():
            raise ValueError("press needs `key`")
        if self.action in ("reply", "ask_user", "remember") and not (self.text or "").strip():
            raise ValueError(f"{self.action} needs `text`")
        return self

    @property
    def is_browser_action(self) -> bool:
        return self.action in BROWSER_ACTIONS

    @property
    def ends_turn(self) -> bool:
        return self.action in TURN_ENDING

    def browser_action(self) -> dict[str, object]:
        """The `action` payload `browser.act@1` sends to the computer."""
        if not self.is_browser_action:
            raise ValueError(f"{self.action} is not a browser action")
        out: dict[str, object] = {"type": self.action}
        for name in ("element", "url", "text", "key", "option", "direction", "seconds"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.action == "type":
            out["submit"] = self.submit
        return out


BOT_STEP_V1 = SCHEMAS.register(BotStep, version=1)


# --- the approval gate -----------------------------------------------------------------


Decision = Literal["ask", "allow"]


@dataclass(frozen=True, slots=True)
class BotRule:
    """`action_type` is a step action or `*`; `host` is a suffix match, or empty for any."""

    action_type: str
    host: str
    decision: Decision

    def matches(self, action_type: str, host: str) -> bool:
        if self.action_type not in ("*", action_type):
            return False
        if not self.host:
            return True
        wanted = self.host.lower().lstrip(".")
        return host == wanted or host.endswith("." + wanted)


@dataclass(frozen=True, slots=True)
class GateDecision:
    ask: bool
    reason: str


_SECRET_FIELD = re.compile(r"pass(word)?|secret|token|otp|2fa|cvv|cvc|card.?number|pin\b", re.I)


def is_secret_field(element: dict[str, object] | None) -> bool:
    """A password input, or a field whose label says it holds a secret."""
    if element is None:
        return False
    if element.get("type") == "password":
        return True
    return bool(_SECRET_FIELD.search(" ".join(str(element.get(k, "")) for k in ("label", "role"))))


def masked(action: dict[str, object]) -> dict[str, object]:
    """The action as a person may see it: a typed secret becomes dots."""
    if not action.get("secret"):
        return action
    out = dict(action)
    out["text"] = "•" * min(12, len(str(action.get("text", ""))))
    return out


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def needs_approval(
    step: BotStep,
    *,
    page_url: str,
    element: dict[str, object] | None,
    rules: tuple[BotRule, ...],
) -> GateDecision:
    """Should this step wait for a person?

    Order: an `ask` rule → ask. An `allow` rule → go (it is the person's standing
    "Always allow"). Otherwise the defaults: typing into a secret-looking field asks,
    and a step the model itself flagged `sensitive` asks. Everything else goes.
    """
    if not step.is_browser_action:
        return GateDecision(False, "not a browser action")
    host = host_of(step.url or "") if step.action == "navigate" else host_of(page_url)

    for rule in rules:
        if rule.decision == "ask" and rule.matches(step.action, host):
            where = f" on {rule.host}" if rule.host else ""
            return GateDecision(True, f"your rule: ask before {rule.action_type}{where}")
    for rule in rules:
        if rule.decision == "allow" and rule.matches(step.action, host):
            return GateDecision(False, "allowed by your rule")

    if step.action == "type" and is_secret_field(element):
        return GateDecision(True, "typing into what looks like a password or secret field")
    if step.sensitive:
        return GateDecision(True, "the bot marked this action as having real consequences")
    return GateDecision(False, "routine")


# --- identities --------------------------------------------------------------------------


def message_id(run_id: object, step: int, kind: str) -> uuid.UUID:
    """A row a run writes. Same run, same step, same kind → same id, so a replay is a
    no-op (`ON CONFLICT DO NOTHING`) rather than a duplicated line in the transcript."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botmsg:{run_id}:{step}:{kind}")


def pending_id(run_id: object, step: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botpending:{run_id}:{step}")


_SLUG = re.compile(r"[^a-z0-9]+")


def actor_name_for(name: str, suffix: str) -> str:
    """`Sales Scout` + `3f9a` → `bot-sales-scout-3f9a`. Unique per bot, stable for life:
    the actor name is what budgets, audit rows and kill switches are keyed on, so a
    renamed bot keeps its actor rather than acquiring a new one."""
    slug = _SLUG.sub("-", name.lower()).strip("-")[:40] or "bot"
    return f"bot-{slug}-{suffix}"


def trim_memory(memory: str, note: str) -> str:
    """Append a note, dropping the oldest lines once over `MAX_MEMORY_CHARS`."""
    note = " ".join(note.split()).removeprefix("- ")
    lines = [line for line in memory.splitlines() if line.strip()]
    if note and f"- {note}" not in lines:
        lines.append(f"- {note}")
    while lines and sum(len(line) + 1 for line in lines) > MAX_MEMORY_CHARS:
        lines.pop(0)
    return "\n".join(lines)
