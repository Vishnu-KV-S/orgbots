"""Bot templates — a bot's setup as something another person can start their own from.

A template is what makes a bot *that* bot to someone who has never met it: its name,
label, description and body, its brief, its approval rules, its routines and whether
Auto Review is on. It is never what the bot has learned or been given — no memories,
no conversation, no saved sign-ins, no files, no connected apps' tokens, no webhook
addresses or secrets. It travels as a JSON file (export and import) or as a link to a
snapshot kept by this runtime (`bot_template_shares`).

**A template is someone else's text and someone else's permissions**, so importing one
is not a copy (`BotManager.duplicate` is the copy, inside an organization):

- *allow* rules are left out unless the person importing says so, having seen them
  (`ImportPlan.allows`): a template must not be able to give the bot it creates
  permission to spend money or delete things on a site without the person who runs
  it deciding that;
- every routine arrives **paused** — nothing a stranger scheduled starts running on its
  own; an event routine gets its own new address, and a signed one needs its secret
  set again;
- the person sees all of it before the bot exists (the preview is the same plan the
  import carries out).

The format is versioned. A file from a newer runtime is refused with that reason rather
than half-understood.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runtime.domain.bot_memory import BotBrief
from runtime.domain.bots import BotAppearance, Decision
from runtime.domain.routines import MAX_ROUTINES_PER_BOT, RoutineSpec

FORMAT: Literal["agent-org/bot-template"] = "agent-org/bot-template"
VERSION = 1
MAX_RULES = 200


class TemplateRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(min_length=1, max_length=32)
    host: str = Field(default="", max_length=253)
    decision: Decision


class BotTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["agent-org/bot-template"] = FORMAT
    version: int = VERSION
    name: str = Field(min_length=1, max_length=80)
    label: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=2_000)
    avatar: str = Field(default="", max_length=16)
    appearance: BotAppearance | None = None
    brief: BotBrief = Field(default_factory=BotBrief)
    auto_review: bool = False
    rules: list[TemplateRule] = Field(default_factory=list, max_length=MAX_RULES)
    routines: list[RoutineSpec] = Field(default_factory=list, max_length=MAX_ROUTINES_PER_BOT)


class TemplateError(ValueError):
    """A template that cannot be imported. The message is shown to the person."""


def parse(raw: object) -> BotTemplate:
    """A template from a file or a request, or `TemplateError` saying, in words, why it
    is not one this runtime can import. The version is checked before the shape, so a
    newer file says it is newer instead of listing the fields it does not know."""
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise TemplateError("this is not a bot template")
    version = raw.get("version")
    if not isinstance(version, int) or version < 1:
        raise TemplateError("this bot template has no version this app can read")
    if version > VERSION:
        raise TemplateError(
            f"this template was made by a newer version of the app (format {version}; "
            f"this one reads up to {VERSION})"
        )
    try:
        template = BotTemplate.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or 'template'}: {e['msg']}"
            for e in exc.errors()[:3]
        ]
        raise TemplateError("this bot template is damaged — " + "; ".join(problems)) from exc
    return readable(template)


def readable(template: BotTemplate) -> BotTemplate:
    """The template as this runtime will import it, or `TemplateError` saying why not."""
    if template.version > VERSION:
        raise TemplateError(
            f"this template was made by a newer version of the app (format {template.version}; "
            f"this one reads up to {VERSION})"
        )
    names = [r.name.strip().lower() for r in template.routines]
    if len(names) != len(set(names)):
        raise TemplateError("two of its routines have the same name")
    return template


class ImportPlan(BaseModel):
    """What importing a template will do — shown before, carried out after."""

    rules: list[TemplateRule]
    """The rules that will be written: every *ask* and *deny*, and the *allow* rules
    only when the person chose to keep them."""
    allows: list[TemplateRule]
    """The template's *allow* rules — for the person to see, and choose."""
    routines: list[RoutineSpec]
    """Every routine, paused."""


def plan(template: BotTemplate, *, keep_allows: bool) -> ImportPlan:
    allows = [r for r in template.rules if r.decision == "allow"]
    rules = [r for r in template.rules if keep_allows or r.decision != "allow"]
    seen: set[tuple[str, str]] = set()
    unique: list[TemplateRule] = []
    for rule in rules:
        key = (rule.action_type, rule.host.strip().lower())
        if key not in seen:
            seen.add(key)
            unique.append(rule.model_copy(update={"host": key[1]}))
    return ImportPlan(
        rules=unique,
        allows=allows,
        routines=[r.model_copy(update={"active": False}) for r in template.routines],
    )


def file_name(template: BotTemplate) -> str:
    """`Research Scout.bot.json` — the name a download is saved as."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in template.name).strip()
    return f"{safe or 'bot'}.bot.json"
