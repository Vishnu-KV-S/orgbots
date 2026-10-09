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
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.bot_memory import BriefPatch, MemoryKind
from runtime.domain.routines import RoutineDraft
from runtime.domain.schemas import SCHEMAS
from runtime.domain.skills import SkillDraft

MAX_STEPS = 24
"""Steps per run — one chunk of a turn. A task that needs more carries on in a new run
(`bot.continue`, up to `Settings.bot_auto_continue_chunks` chunks), or, past that or
with auto-continue off, reports progress and waits for "continue"."""

BOT_TURN_PRIORITY = 80
"""Queue order, not admission. A bot turn is someone waiting at the chat box, so it is
claimed ahead of background work (cron firings, the department loop: the default 50).
Without it a scheduler catching up after downtime puts a "hi" behind every backlogged
research run, and with one worker slot that is hours. A long task's next chunk is the
same person's same turn, so it gets the same priority."""

MAX_HISTORY_MESSAGES = 24
"""Conversation messages (person and bot, not activity) carried into a run's input."""

MAX_HELPERS = 5
"""Live helpers one bot may have created. A bot that wants a sixth reuses one."""

MAX_HELPER_DEPTH = 2
"""A person's bot is depth 0; its helper 1; the helper's helper 2, and no deeper.
The same bound as the runtime's delegation depth, which is what `ask_bot` rides on —
a bot that could create a helper it could never ask anything would be a bot that
creates litter."""

HELPER_REPLY_CHARS = 2_000
"""How much of a helper's answer is carried back into the asking bot's next prompt."""

MAX_PLAN_ITEMS = 10
PLAN_ITEM_CHARS = 200
NOTES_CHARS = 1_500
"""Working memory — a turn's plan and notes, rewritten by the model as it goes. Small on
purpose: it rides in every step's output, and a step that overflows `max_output_tokens`
is a malformed step."""

BOT_DELEGATION = {
    "enabled": True,
    "max_depth": MAX_HELPER_DEPTH,
    # One question at a time per bot — a turn is a sequence of steps, so a bot never
    # has two questions outstanding — and a tree-wide bound on how much can be live.
    "max_children": 1,
    "max_live_descendants": MAX_HELPER_DEPTH,
    "max_subtree_cost_cents": 600,
    "max_subtree_llm_calls": 150,
    "exhaustion_policy": "drain",
}
"""Every bot actor's delegation limits (`actors.delegation`), written when the bot is
created. The same for every bot, like its spec: what a bot may delegate is not
something a bot or its instructions can widen."""

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

ROUTINE_ACTIONS = frozenset({"save_routine", "delete_routine"})
"""Steps on the bot's own routines (`domain.routines`) — runtime state, like memory."""

SKILL_ACTIONS = frozenset({"save_skill", "use_skill"})
"""Steps on the organization's skills library (`domain.skills`)."""

FILE_ACTIONS = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "append_file",
        "edit_file",
        "move_file",
        "delete_file",
    }
)
"""Steps on the team's shared drive (`domain.files`). Like memory, the drive is the
runtime's own state, so these are not tool calls: nothing leaves the database."""

MAX_SEED_MEMORIES = 10
"""What a bot may hand a helper it creates, as the helper's first memories."""
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
    "create_bot",
    "ask_bot",
    "recall",
    "forget",
    "update_brief",
    "sign_in",
    "list_files",
    "read_file",
    "write_file",
    "append_file",
    "edit_file",
    "move_file",
    "delete_file",
    "look",
    "save_routine",
    "delete_routine",
    "save_skill",
    "use_skill",
    "run_command",
    "copy_file",
]


class BotStep(BaseModel):
    """One decision. Exactly one action per step, and a reason for it."""

    model_config = ConfigDict(extra="forbid")

    thought: str = Field(
        max_length=1_200,
        description="Brief reasoning: what you see, what you are trying to do, and why "
        "this action is the next one. Shown to the person as your activity.",
    )
    plan: list[Annotated[str, Field(max_length=PLAN_ITEM_CHARS)]] = Field(
        default_factory=list,
        max_length=MAX_PLAN_ITEMS,
        description="Your plan for a task that takes more than a couple of steps: short "
        "checklist lines, each starting [x] done, [>] doing now or [ ] to do. Write it on "
        "the first step of such a task and rewrite it whenever it changes; an empty list "
        "keeps the plan you already have. Leave it empty for a plain reply.",
    )
    notes: str | None = Field(
        default=None,
        max_length=NOTES_CHARS,
        description="Your working notes for this task — what you have found so far (names, "
        "numbers, prices, links) and what you still need. Pages are gone once you leave "
        "them, so anything you will need later goes here. Replaces your previous notes: "
        "carry forward what still matters, concisely. Omit to keep the notes you have.",
    )
    action: StepAction = Field(
        description="navigate/click/type/press/select/scroll/hover/back/forward/reload/"
        "wait drive the browser. observe re-reads the page. reply ends your turn with a "
        "message to the person. ask_user ends your turn with a question you need answered. "
        "MEMORY: remember saves something to long-term memory and continues (text = the "
        "memory, memory_kind, importance; memory = an [id] to revise an existing one; bot = "
        "one of your helpers to teach it instead of yourself). forget deletes a memory "
        "(memory = its [id]; bot = a helper's name to edit its memory). recall searches your "
        "whole memory and past conversations (text = what to look for). BRIEF: update_brief "
        "changes a primary instruction — yours, or a helper's with bot = its name (brief = "
        "only the fields that change, text = why). TEAM: create_bot creates a helper bot "
        "under you (bot = its name, label = job title, text = its mission, brief = its full "
        "job brief, seed_memories = facts it should start out knowing). ask_bot gives one of "
        "your helpers a task and waits for its answer (bot = the helper's name, text = the "
        "task, with every fact it needs). SIGN-IN: sign_in hands a login, sign-up or "
        "verification-code form to the runtime, which fills it from your person's vault or "
        "asks them with a secure form (element = optional, any field of that form); you never "
        "see or type the values. FILES (your team's shared drive): list_files lists a folder "
        "(path = the folder, default /; or text = words to search file names and contents "
        "for). read_file reads a file (path; from_line to continue a long one). write_file "
        "creates a file, or replaces one you have read this turn (path, text = the whole "
        "content). append_file adds text at the end, creating the file if needed (path, "
        "text). edit_file replaces one passage (path, find = the exact text now in the file, "
        "text = what replaces it). move_file renames or moves a file (path, to). delete_file "
        "deletes one (path). VISION: look asks a vision model about what is on your screen "
        "right now (text = your question) — for images, charts, maps, colours and layout, "
        "or whenever the page listing does not explain what you see; with path = an image "
        "file in your team drive (an attachment, say) it looks at that instead. The answer "
        "comes back to you. ROUTINES (recurring work, only when your person asks for it): "
        "save_routine creates or changes one of your routines by name (routine = name, "
        "instruction, cron, timezone, output; active=false pauses it). delete_routine "
        "removes one (routine = its name). SKILLS (your organization's shared how-tos): "
        "use_skill loads a skill so you can follow it (skill = its name). save_skill "
        "saves a procedure to the library when your person asks you to keep one, or "
        "after they show you a task (skill = name, title, when, inputs, steps, checks, "
        "output, approvals); an existing name changes that skill. TERMINAL: run_command "
        "runs a shell command (text = the command, timeout = seconds, up to 300) in your "
        "sandboxed /workspace, which all your person's bots share and browser downloads "
        "land in; local = true runs it on your person's own machine instead, which they "
        "approve first. copy_file copies a file between /workspace and your team drive "
        "(path = the source, to = the destination; one of them starts with /workspace)."
    )
    element: int | None = Field(
        default=None,
        description="The [number] of the element, from the LATEST page listing only. "
        "Required for click, type, select and hover.",
    )
    url: str | None = Field(default=None, description="For navigate.")
    bot: str | None = Field(
        default=None,
        max_length=80,
        description="For create_bot: the new helper's name. For ask_bot: which helper.",
    )
    label: str | None = Field(
        default=None, max_length=80, description="For create_bot: a short job title."
    )
    text: str | None = Field(
        default=None,
        description="The text to type (type), the message (reply / ask_user), the note "
        "to save (remember), or a file's content (write_file, append_file; the "
        "replacement for edit_file).",
    )
    path: str | None = Field(
        default=None,
        max_length=400,
        description="For file actions: a path in your team drive, e.g. "
        "/projects/acme/vendors.csv. For list_files, a folder (default /). For look: an "
        "image file to look at instead of the screen.",
    )
    to: str | None = Field(
        default=None,
        max_length=400,
        description="For move_file: the new path, or a folder ending in / to move it into. "
        "For copy_file: where the copy goes (a folder ending in / keeps the name).",
    )
    find: str | None = Field(
        default=None,
        description="For edit_file: the passage to replace, copied exactly from the file as "
        "it is now — enough of it to match one place only.",
    )
    from_line: int | None = Field(
        default=None, ge=1, description="For read_file: the line to start reading from."
    )
    memory: str | None = Field(
        default=None,
        max_length=12,
        description="For remember (to revise) and forget: a memory's [id] from your memory.",
    )
    memory_kind: MemoryKind = Field(
        default="fact",
        description="For remember: preference (how your person likes things), person (someone "
        "and how to reach them), fact, skill (how to do something — steps that worked), or "
        "episode (something that happened).",
    )
    importance: int = Field(
        default=3,
        ge=1,
        le=5,
        description="For remember and diary: 1 trivial … 5 essential. Preferences at 4+ are in "
        "every prompt.",
    )
    brief: BriefPatch | None = Field(
        default=None,
        description="For update_brief: the fields that change (lists are replaced whole). "
        "For create_bot: the helper's job brief.",
    )
    seed_memories: list[str] = Field(
        default_factory=list,
        max_length=MAX_SEED_MEMORIES,
        description="For create_bot: facts the helper should start out knowing.",
    )
    routine: RoutineDraft | None = Field(
        default=None,
        description="For save_routine: the routine (name, and for a new one instruction "
        "and cron). For delete_routine: its name.",
    )
    skill: SkillDraft | None = Field(
        default=None,
        description="For use_skill: the skill's name. For save_skill: the skill — name, "
        "and for a new one its steps and the rest.",
    )
    diary: str | None = Field(
        default=None,
        max_length=600,
        description="For reply and ask_user: one line for your diary — what this turn was "
        "about and how it ended, with names, numbers and links worth remembering.",
    )
    key: str | None = Field(default=None, description="For press, e.g. Enter, Tab, Escape.")
    option: str | None = Field(default=None, description="For select: the option's label.")
    direction: Literal["up", "down"] | None = Field(default=None, description="For scroll.")
    seconds: float | None = Field(default=None, ge=0, le=10, description="For wait.")
    timeout: int | None = Field(
        default=None, ge=1, le=300, description="For run_command: seconds before it is stopped."
    )
    local: bool = Field(
        default=False,
        description="For run_command: run on your person's own machine instead of the "
        "sandbox. Only when the sandbox cannot do it; they approve each one.",
    )
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
        if (
            self.action in ("reply", "ask_user", "remember", "recall", "look")
            and not (self.text or "").strip()
        ):
            raise ValueError(f"{self.action} needs `text`")
        if self.action == "forget" and not (self.memory or "").strip():
            raise ValueError("forget needs `memory` — the [id] of the memory to forget")
        if self.action == "update_brief":
            if self.brief is None or self.brief.is_empty():
                raise ValueError("update_brief needs `brief` with at least one field to change")
            if not (self.text or "").strip():
                raise ValueError("update_brief needs `text` — why the brief is changing")
        if self.action in FILE_ACTIONS - {"list_files"} and not (self.path or "").strip():
            raise ValueError(f"{self.action} needs `path` — e.g. /notes/todo.md")
        if self.action == "write_file" and self.text is None:
            raise ValueError("write_file needs `text` — the file's whole content")
        if self.action == "append_file" and not self.text:
            raise ValueError("append_file needs `text` — what to add")
        if self.action == "edit_file":
            if not self.find:
                raise ValueError("edit_file needs `find` — the exact passage to replace")
            if self.text is None:
                raise ValueError("edit_file needs `text` — the replacement (empty to delete it)")
        if self.action == "move_file" and not (self.to or "").strip():
            raise ValueError("move_file needs `to` — the new path, or a folder ending in /")
        if self.action in ROUTINE_ACTIONS and self.routine is None:
            raise ValueError(f"{self.action} needs `routine` — at least its name")
        if self.action == "run_command" and not (self.text or "").strip():
            raise ValueError("run_command needs `text` — the command to run")
        if self.action == "copy_file" and not (
            (self.path or "").strip() and (self.to or "").strip()
        ):
            raise ValueError("copy_file needs `path` (the source) and `to` (the destination)")
        if self.action in SKILL_ACTIONS and self.skill is None:
            raise ValueError(f"{self.action} needs `skill` — at least its name")
        if self.action in ("create_bot", "ask_bot"):
            if not (self.bot or "").strip():
                raise ValueError(f"{self.action} needs `bot` — the helper's name")
            if not (self.text or "").strip():
                raise ValueError(
                    "create_bot needs `text` — the helper's role"
                    if self.action == "create_bot"
                    else "ask_bot needs `text` — the task"
                )
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


BOT_STEP = SCHEMAS.register(BotStep, version=8)
"""Version 8 added the terminal (`run_command` with `timeout` and `local`, `copy_file`).
Version 7 let `look` take a `path` (an image in the team drive). Version 6 added skills
(`save_skill`, `use_skill`, with `skill`). Version 5 added routines (`save_routine`,
`delete_routine`, with `routine`). Version 4 added the team drive (`list_files` …
`delete_file`, with `path`, `to`, `find` and `from_line`) and `look` (vision: a question
about the screen). Version 3 added working memory (`plan` and `notes`, carried from step
to step within a turn) and `sign_in` (the login vault). Version 2 added memory (remember
kinds, forget, recall, diary) and the brief (update_brief, create_bot's brief and seed
memories). Version 1 was never run against stored data, so it is not kept."""


# --- appearance -------------------------------------------------------------------------


class BotAppearance(BaseModel):
    """A bot's 3D body. Presentation only — nothing here reaches a prompt or a spec.

    Closed vocabularies rather than free strings, because the UI builds the model from
    these names and an unknown one would render as nothing. Colours are `#rrggbb`.
    """

    model_config = ConfigDict(extra="forbid")

    shape: Literal["orb", "cube", "capsule", "pod", "tv"] = "orb"
    body: str = Field(default="#e9e4f2", pattern=r"^#[0-9a-fA-F]{6}$")
    glow: str = Field(default="#e040fb", pattern=r"^#[0-9a-fA-F]{6}$")
    eyes: Literal["pill", "round", "square", "visor", "dot"] = "pill"
    top: Literal["ring", "knobs", "antenna", "ears", "halo", "none"] = "ring"
    finish: Literal["gloss", "matte", "metal", "pearl"] = "gloss"


# --- the approval gate -----------------------------------------------------------------


Decision = Literal["ask", "allow", "deny"]


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
    deny: bool = False
    """A "never allow" rule: the step is refused, not parked."""


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
    """Should this step wait for a person — or not happen at all?

    Order: a `deny` rule → refused. An `ask` rule → ask. An `allow` rule → go (it is
    the person's standing "Always allow"). Otherwise the defaults: typing into a
    secret-looking field asks, a command on the person's own machine asks, and a step
    the model itself flagged `sensitive` asks. Everything else goes.

    A command is judged as `run_command` (the sandbox) or `run_local` (the person's
    machine) — two rule kinds, because "always allow in the sandbox" must never be read
    as "always allow on my laptop".
    """
    if step.action == "run_command":
        kind = "run_local" if step.local else "run_command"
        for decision, reason in (("deny", "never"), ("ask", "ask before"), ("allow", "")):
            for rule in rules:
                if rule.decision == decision and rule.matches(kind, ""):
                    if decision == "allow":
                        return GateDecision(False, "allowed by your rule")
                    where = "on your machine" if step.local else "in the sandbox"
                    return GateDecision(
                        decision == "ask",
                        f"your rule: {reason} running commands {where}",
                        deny=decision == "deny",
                    )
        if step.local:
            return GateDecision(True, "the command runs on your own machine, not the sandbox")
        if step.sensitive:
            return GateDecision(True, "the bot marked this command as having real consequences")
        return GateDecision(False, "routine")
    if not step.is_browser_action:
        return GateDecision(False, "not a browser action")
    host = host_of(step.url or "") if step.action == "navigate" else host_of(page_url)

    for rule in rules:
        if rule.decision == "deny" and rule.matches(step.action, host):
            where = f" on {rule.host}" if rule.host else ""
            return GateDecision(False, f"your rule: never {rule.action_type}{where}", deny=True)

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


def helper_id(run_id: object, step: int) -> uuid.UUID:
    """A helper created by a run's step. Derived, so a replayed step finds the helper it
    already made instead of making a second one."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"bothelper:{run_id}:{step}")


def pending_id(run_id: object, step: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botpending:{run_id}:{step}")


_SLUG = re.compile(r"[^a-z0-9]+")


def actor_name_for(name: str, suffix: str) -> str:
    """`Sales Scout` + `3f9a` → `bot-sales-scout-3f9a`. Unique per bot, stable for life:
    the actor name is what budgets, audit rows and kill switches are keyed on, so a
    renamed bot keeps its actor rather than acquiring a new one."""
    slug = _SLUG.sub("-", name.lower()).strip("-")[:40] or "bot"
    return f"bot-{slug}-{suffix}"
