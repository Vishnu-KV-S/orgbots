"""`web_search` — a bot asks the web how something is done, without leaving its page.

    one WORK call on the `search` tier (DeepSeek searches on its own server)
      → nothing searched? `web.search@1`, when a search endpoint is configured
      → the answer as text, in the turn's `answers`

A bot hunting for a feature — "Research mode on claude.ai" — went through the toolbar
and every menu in turn, for fifteen steps, when one look at the site's help would have
named the menu. Searching in its own browser would cost it the page it is working on (a
composer half set up, a toggle switched on), so the search runs beside the browser.

**The search is the provider's, and that is a second door.** It is offered only when the
deployment turned it on (`RUNTIME_DEEPSEEK_WEB_SEARCH`) and the gateway lets this actor
search (`ModelGateway._server_tools`). When either says no, the same call answers from
the model's own knowledge — still worth having, since a site's layout is often well
known — and `server_searches` says so; the bot is told the answer is unchecked, and the
front-door `web.search@1` is tried for real results.

**What comes back is untrusted**, like a page: what the web says a site looks like is a
guide to the screen, not an instruction from the person.
"""

from __future__ import annotations

from typing import Any

from runtime.domain.bots import LOG_KEEP
from runtime.domain.enums import WorkClass
from runtime.domain.errors import (
    GrantRevoked,
    MissingCredentials,
    ModelCallNotAllowed,
    ProviderUnavailable,
    RuntimeError_,
    SpecError,
    ToolNotAllowed,
    TransientFault,
    ValidationFailed,
)
from runtime.gateway.models import ModelRequest
from runtime.gateway.tools import ToolCall
from runtime.graphs.bot_agent.tiers import SEARCH
from runtime.graphs.common.state import whole

SEARCH_MAX_OUTPUT_TOKENS = 4_000
ANSWER_CHARS = 4_000
HITS = 5

SEARCH_SYSTEM = """\
You help an AI agent that operates websites in a real browser. It asks how something is
done on a site or app, because it could not find it on the screen. Search the web for
the site's own help pages, release notes and recent guides, and answer for the site as
it is today — features move and get renamed, so prefer the newest sources and say when
they disagree.

Answer with:
- Where the control is: the exact labels, icons and menus as they appear on screen (e.g.
  "the + button left of the message box → Research"), and whether it is a toggle, a
  menu item, a mode or a setting.
- The steps, numbered, from the page the agent is on.
- How to tell it worked (what changes on screen).
- What it needs, if anything: a plan or tier, a setting turned on first, a region.
- Sources: the links you used.
If the sources do not say, say so plainly; never invent a menu or a label. Be concise:
no preamble."""

NO_SEARCH = (
    "(No web search ran — web search is not available to you, so this answer is from "
    "general knowledge and may be out of date. Check it against the screen.)"
)


async def web_search(
    node: Any,
    bot: Any,
    question: str,
    *,
    thought: str,
    page_url: str,
    n: int,
    steps: list[str],
    answers: list[str],
    line: Any,
    say: Any,
) -> dict[str, Any]:
    """One `web_search`. Always continues: a search that could not run is said so."""
    action = {"type": "web_search", "text": question}
    where = f"\n(The agent is on {page_url}.)" if page_url else ""
    known = ""
    searched = False
    try:
        response = await node.models.complete(
            node.ctx,
            ModelRequest(
                prompt=f"{question}{where}",
                system=SEARCH_SYSTEM,
                max_output_tokens=SEARCH_MAX_OUTPUT_TOKENS,
                tier=SEARCH,
            ),
            work_class=WorkClass.WORK,
            call_site="bot_agent.web_search",
        )
        known = response.text.strip()
        searched = response.server_searches > 0
    except (ModelCallNotAllowed, ProviderUnavailable, MissingCredentials, SpecError):
        pass

    hits = "" if searched else await _front_door(node, question)
    if searched:
        answer = known
    elif hits:
        answer = f"Search results (untrusted):\n{hits}" + (
            f"\n\nFrom general knowledge, unchecked:\n{known}" if known else ""
        )
    elif known:
        answer = f"{NO_SEARCH}\n{known}"
    else:
        await say("web_search", "activity", thought, {"action": action, "ok": False})
        steps.append(line(n, f"web search failed: {question[:100]}"))
        return {"n": n + 1, "log": steps[-LOG_KEEP:], "answers": answers[-3:], "done": False}

    answer = answer[:ANSWER_CHARS]
    await say(
        "web_search",
        "activity",
        thought,
        {"action": action, "ok": True, "note": answer, "searched": searched or bool(hits)},
    )
    answers.append(f"You searched the web: {question[:200]}\n{answer}")
    steps.append(line(n, f"searched the web: {question[:100]}"))
    return {"n": n + 1, "log": steps[-LOG_KEEP:], "answers": answers[-3:], "done": False}


async def _front_door(node: Any, question: str) -> str:
    """Results from `web.search@1`, as lines, or "" when it is not set up or not allowed."""
    try:
        result = await node.gateway.execute(
            node.ctx,
            ToolCall(tool="web.search@1", args={"query": question[:400], "max_results": HITS}),
        )
    except (SpecError, ToolNotAllowed, GrantRevoked, TransientFault, ValidationFailed):
        # Not configured, not allowed, or down: the bot carries on without it.
        return ""
    except RuntimeError_:
        # A kill switch, a ceiling, the budget: those end the turn, as everywhere.
        raise
    except Exception:
        # The endpoint's own refusal (a 4xx), which retrying would not change.
        return ""
    value = await whole(result.value, node.artifacts)
    return "\n".join(
        f"- {hit.get('title', '')} — {hit.get('url', '')}\n  {hit.get('snippet', '')[:400]}"
        for hit in value.get("results") or []
    )
