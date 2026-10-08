"""Group chats and bots messaging bots — the values. Pure.

**A group** is a conversation between a person and two to six of their bots, for work
that needs several of them: one finds the leads, another drafts the emails. The person
says who owns a request with `@Name` (`@everyone` for all of them); a message naming
nobody goes to the group's lead. A bot answers in the group, and hands a part to a
teammate by naming them there. A reply can start a **thread** under one message, so
feedback on one result does not scatter through the main conversation.

**A message to another bot** (`message_bot`) is asynchronous, unlike `ask_bot`, which
gives a helper a task and waits: the recipient picks it up when it is free, works on
it in its own turn, and its reply comes back to the sender as a message of its own —
which wakes the sender to carry on. With `handoff` the recipient *owns* the task from
then on and reports to the person itself; nothing comes back.

**Bots talking to bots can talk forever**, so every message between them carries a hop
count: a reply is one more hop than what it answers, a hand-off on a group's thread is
one more than the message it was in, and past `MAX_HOPS` nothing more is delivered. A
bot also has a daily allowance of messages to other bots. A reply to a reply never
asks for one back.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

MIN_MEMBERS = 2
MAX_MEMBERS = 6
MAX_HOPS = 4
"""How many bot-to-bot deliveries one person's message can set off, end to end."""

DAILY_BOT_MESSAGES = 60
"""Messages one bot may send other bots in a day (`message_bot`, and @mentions in a group)."""

GROUP_HISTORY = 30
"""Group messages a bot reads on its turn."""

MAX_WAIT_HOURS = 6
"""How long a delivery waits for a busy bot before it is dropped with a note."""

EVERYONE = ("everyone", "all")


@dataclass(frozen=True, slots=True)
class Member:
    bot_id: uuid.UUID
    name: str


def mentions(text: str, members: Sequence[Member]) -> tuple[list[uuid.UUID], bool]:
    """Which members a message names, in order, and whether it said `@everyone`.

    Names can have spaces ("Sales Scout"), so this matches the members' own names
    rather than guessing where a mention ends — longest first, so `@Ana Lee` is not
    read as `@Ana` when both exist.
    """
    lowered = text.lower()
    everyone = any(re.search(rf"(?<!\w)@{word}\b", lowered) for word in EVERYONE)
    found: list[tuple[int, uuid.UUID]] = []
    taken: list[tuple[int, int]] = []
    for member in sorted(members, key=lambda m: -len(m.name)):
        pattern = re.compile(rf"(?<!\w)@{re.escape(member.name.lower())}(?![\w])")
        for match in pattern.finditer(lowered):
            span = match.span()
            if any(a < span[1] and span[0] < b for a, b in taken):
                continue
            taken.append(span)
            found.append((span[0], member.bot_id))
            break
    ordered: list[uuid.UUID] = []
    for _, bot_id in sorted(found, key=lambda pair: pair[0]):
        if bot_id not in ordered:
            ordered.append(bot_id)
    return ordered, everyone


def recipients(
    text: str, members: Sequence[Member], *, lead: uuid.UUID | None, author: uuid.UUID | None
) -> list[uuid.UUID]:
    """Who a group message is for. A person's message naming nobody goes to the lead;
    a bot's message naming nobody goes to nobody (it is an answer, not a request)."""
    named, everyone = mentions(text, members)
    if everyone:
        named = [m.bot_id for m in members]
    elif not named and author is None and lead is not None:
        named = [lead]
    return [b for b in named if b != author]


def wake_id(*parts: object) -> uuid.UUID:
    """One delivery. Derived from what caused it, so a replayed step delivers once."""
    return uuid.uuid5(uuid.NAMESPACE_URL, "botwake:" + ":".join(str(p) for p in parts))


def group_line(author: str, content: str, *, thread: bool = False) -> str:
    return f"{'  ↳ ' if thread else ''}{author}: {content.strip()}"
