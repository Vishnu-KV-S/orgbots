"""Which model a bot's next step gets — by how hard it is.

    quick  (Flash)             a "hi", waiting on a slow page, a step the bot called easy
    normal (Pro)               everything else
    think  (Pro, thinking on)  the bot is stuck, or redoing what its person corrected

Most of a turn is routine, and paying for the hardest step on every one is waste in one
direction while a stuck bot that never thinks harder is waste in the other: the same
cheap guess, again and again. So each pass is placed from two sources:

- **What happened**, read from the turn itself: an unreadable last decision, going
  round in circles, an action that failed or changed nothing, a person saying "not
  this" — before the turn or into it as it works. Evidence, so it decides upward and
  nothing overrides it downward.
- **The bot's own word** (`BotStep.next_step`): the step before says how hard it
  expects the next to be — it knows "now I just wait" or "I have no idea where this
  lives" better than any rule. It can ask for either tier; quick only when nothing in
  the evidence says otherwise.

Pure: a function of the turn's state, so a test can pin every placement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

QUICK = "quick"
THINK = "think"
SEARCH = "search"
"""Not a step's tier: the one a `web_search` call runs on (`search`), whose model
searches the web on its provider's server."""

_SMALL_TALK = re.compile(
    r"^\s*(hi+|hello|hey|yo|thanks|thank you|thx|ty|ok(ay)?|cool|nice|great|got it|"
    r"good (morning|afternoon|evening|night)|bye|see you|👍|🙏)\W*\s*$",
    re.I,
)
_CORRECTION = re.compile(
    r"\b(not (this|that|what)|wrong|again|still|instead|i said|i mean|i meant|didn'?t|"
    r"doesn'?t|isn'?t|aren'?t|why (not|are you|you)|no,|that'?s not)\b",
    re.I,
)
_TROUBLE = ("FAILED", "nothing on the page changed", "NOT DONE", "refused", "could not be read")
_WAITING = ("wait ", "looked at the page again")


@dataclass(frozen=True, slots=True)
class Placement:
    tier: str | None
    """`quick`, `think`, or None for the actor's usual `WORK` profile."""
    reason: str


def place(
    *,
    n: int,
    steps: list[str],
    request: str,
    confused: int = 0,
    repeated: str = "",
    hint: str | None = None,
    has_plan: bool = False,
    check_in: bool = False,
    steered: bool = False,
) -> Placement:
    """The tier for the pass about to decide.

    `steps` is the turn's step log, `request` the latest message from the person (or
    whoever started the turn), `hint` what the previous step said of this one, and
    `steered` that the person has just sent a message into the turn as it worked.
    """
    last = steps[-1] if steps else ""
    if confused:
        # Down, not up: the thinking model on a long page is the one that drifts out of
        # JSON (into `<action>…</action>` tags, every try). A plainer model gets the
        # same step log, with what was wrong, and decides it.
        if confused >= 2:
            return Placement(QUICK, "two decisions in a row could not be read")
        return Placement(None, "the last decision could not be read")
    if repeated:
        return Placement(THINK, "going round in circles")
    if any(mark in last for mark in _TROUBLE):
        return Placement(THINK, "the last step did not work")
    if steered:
        return Placement(THINK, "the person changed the task as it was worked on")
    if n == 0 and _CORRECTION.search(request) and not _SMALL_TALK.match(request):
        return Placement(THINK, "the person corrected an earlier attempt")
    if hint == "hard":
        return Placement(THINK, "the bot expected a hard step")

    if n == 0 and not has_plan and not check_in and _SMALL_TALK.match(request):
        return Placement(QUICK, "small talk")
    if hint == "easy":
        return Placement(QUICK, "the bot expected an easy step")
    if n > 0 and last.split(". ", 1)[-1].startswith(_WAITING):
        return Placement(QUICK, "waiting on the page")
    return Placement(None, "a usual step")
