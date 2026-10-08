"""`sign_in` — how a bot gets through a login, sign-up or code page without the secret.

The model's part is one word: it says `sign_in` (optionally pointing at a field of the
form). Everything after that is the runtime's, and none of it puts a value in front
of the model:

1. Read the form off the page (`domain.vault.form_fields`).
2. If the vault can answer every field — fresh values the person just gave this bot,
   or a saved login they allowed to be used automatically — fill it through
   `browser.act@1`'s `fill_credentials`, whose arguments are entry ids and element
   numbers. The bot is told which login was used, by hint (`a••@example.com`).
3. Otherwise put a credential card in the chat: the fields to fill, the site, and the
   saved logins that could do it. The turn ends; the person's submission goes to the
   vault and a fresh run fills the form (`resume`), exactly as an approval resumes.

A plan already tried this turn is not tried again (`FillPlan.signature`): the same
form coming back after a fill is a wrong password, and refilling it is how accounts
get locked. The person is asked instead, with the card saying the last try failed.

A bot that tries to *type* into a password or code box lands here too. Whatever it
meant to type is dropped unread — it can only have come from its prompt, which is
the one place a secret must not be.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from runtime.domain.bots import host_of
from runtime.domain.vault import (
    CredentialField,
    FillPlan,
    answerable,
    asked,
    form_fields,
    plan_fill,
    purpose_of,
    site_of,
    with_next_page,
)
from runtime.gateway.tools import ToolCall
from runtime.graphs.common.state import whole

_PURPOSE = {"sign_in": "sign in to", "sign_up": "create an account on", "verify": "verify on"}


def _rule_says_ask(rules: Any, host: str) -> bool:
    return any(r.decision == "ask" and r.matches("sign_in", host) for r in rules)


async def _fill(
    node: Any,
    bot: Any,
    plan: FillPlan,
    *,
    n: int,
    steps: list[str],
    line: Any,
    say: Any,
    host: str,
    tried: list[str],
    thought: str,
    kind: str,
    shown: str,
) -> dict[str, Any]:
    result = await node.gateway.execute(
        node.ctx,
        ToolCall(
            tool="browser.act@1",
            args={
                "screen_id": str(bot.id),
                "action": {
                    "type": "fill_credentials",
                    "entries": [str(e) for e in plan.entries],
                    "fields": [f.as_dict() for f in plan.fields],
                    "submit": True,
                },
                "label": bot.name,
            },
        ),
    )
    # A busy page's result arrives as an artifact reference; the outcome is in it.
    value = await whole(result.value, node.artifacts)
    tried.append(plan.signature(host))
    await say(
        kind,
        "activity",
        thought,
        {
            "action": {
                "type": "sign_in",
                "host": host,
                "via": plan.via,
                "label": plan.label,
                "fields": sorted({f.kind for f in plan.fields}),
            },
            "ok": value.get("ok"),
            "error": value.get("error"),
            "url": value.get("url"),
            "title": value.get("title"),
        },
    )
    outcome = "ok" if value.get("ok") else f"FAILED: {value.get('error')}"
    steps.append(
        line(
            n,
            f"{shown} → {outcome} (now at {value.get('url', '')}). If the same form is still "
            "showing with its fields (filled), click its submit button.",
        )
    )
    return {"n": n + 1, "log": steps[-12:], "tried": tried, "done": False}


async def sign_in(
    node: Any,
    bot: Any,
    page: dict[str, Any],
    *,
    anchor: int | None,
    thought: str,
    n: int,
    steps: list[str],
    tried: list[str],
    line: Any,
    say: Any,
    end: Callable[[dict[str, Any]], dict[str, Any]],
    delegated_by: str | None,
    working: dict[str, Any] | None = None,
    redirected: bool = False,
) -> dict[str, Any]:
    """One `sign_in` step. Fills from the vault and continues, or asks and ends."""
    bots = node.org.bots
    url = str(page.get("url", ""))
    host = site_of(host_of(url))
    if not host or not url.startswith(("https://", "http://")):
        steps.append(line(n, "sign_in: this is not a web page with a sign-in form"))
        return {"n": n + 1, "log": steps[-12:], "tried": tried, "done": False}
    fields = form_fields(list(page.get("elements", [])), anchor)
    if not fields:
        steps.append(
            line(
                n,
                "sign_in: no sign-in, sign-up or code fields on this page. Open the site's "
                "sign-in page first, then sign_in.",
            )
        )
        return {"n": n + 1, "log": steps[-12:], "tried": tried, "done": False}

    options = await bots.vault_options(bot, host)
    ask_first = _rule_says_ask(await bots.rules(bot.id), host)
    plan = None if ask_first else plan_fill(fields, options, tried=set(tried), host=host)
    if plan is not None:
        shown = (
            f"signed in to {host} with your saved login {plan.label}".strip()
            if plan.via == "saved"
            else f"filled the {host} form with the details the person entered"
        )
        return await _fill(
            node,
            bot,
            plan,
            n=n,
            steps=steps,
            line=line,
            say=say,
            host=host,
            tried=tried,
            thought=thought,
            kind="sign_in",
            shown=shown,
        )

    purpose = purpose_of(fields)
    wanted = [f for f in fields if f.elements]
    every = with_next_page(fields) if purpose == "sign_in" else fields
    card = asked(every)
    retry = any(sig.startswith(f"{host}|") for sig in tried)
    usable = [
        o for o in options if o.kind == "saved" and all(answerable(f.kind, o.kinds) for f in wanted)
    ]
    reason = (
        "the last sign-in did not get past this form — the details may be wrong"
        if retry
        else "your rule: ask before signing in here"
        if ask_first
        else "the page asks for sign-in details the vault does not have"
    )
    if redirected:
        thought = (
            f"{thought}\n(I don't type passwords or codes myself — please enter them here.)"
        ).strip()
    request = await bots.request_credentials(
        bot,
        run_id=node.ctx.run_id,
        step=n,
        host=host,
        page_url=url,
        purpose=purpose,
        fields=every,
        ask={f.key for f in card},
        thought=thought,
        saved=usable,
        retry=retry,
        reason=reason,
        working=working,
    )
    if delegated_by:
        await say(
            "delegated_creds",
            "system",
            f"Waiting for the person to {_PURPOSE[purpose]} {host} before I answer {delegated_by}.",
        )
    return end(
        {
            "status": "awaiting_credentials",
            "steps": n,
            "request_id": str(request),
            "reply": (
                f"I need the person to {_PURPOSE[purpose]} {host} first. They can do it "
                "in my conversation."
            ),
        }
    )


async def resume(
    node: Any,
    bot: Any,
    request_id: str,
    *,
    n: int,
    steps: list[str],
    tried: list[str],
    line: Any,
    say: Any,
    carry: dict[str, Any],
    page: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """The first pass of a run started by the person answering a credential card.

    Returns `(update, note)`: a state update when the form was filled (the pass is
    over), or a note for the model when the person declined.
    """
    request = await node.org.bots.credential_request(uuid.UUID(request_id))
    if request is None:
        return None, ""
    # The run that asked left its plan and notes on the request; this is a new run.
    carry.update(
        plan=list(request.working.get("plan") or []),
        notes=str(request.working.get("notes") or ""),
    )
    if request.status == "cancelled":
        return None, (
            f"The person chose NOT to sign in to {request.host}. Do not try to sign in "
            "there again this turn; carry on without it, or reply saying what you could "
            "not do."
        )
    if request.status != "submitted" or not request.entry_ids:
        return None, ""
    asked_for = [CredentialField.from_dict(raw) for raw in request.fields]
    fields = _on_this_page(asked_for, page, request.host)
    if fields is None:
        # The details are in the vault for ten minutes, scoped to this bot; the next
        # sign_in on the right page fills from them without asking again.
        return None, (
            f"The person entered their sign-in details for {request.host}, but that form is "
            "no longer on the screen. Go back to its sign-in page and use sign_in — the "
            "details are kept for a few minutes."
        )
    plan = FillPlan(
        entries=tuple(request.entry_ids),
        fields=fields,
        via="saved" if request.saved else "once",
    )
    shown = f"(the person entered their details securely) filled the {request.host} form"
    return (
        await _fill(
            node,
            bot,
            plan,
            n=n,
            steps=steps,
            line=line,
            say=say,
            host=request.host,
            tried=tried,
            thought="Filling in the form with the details you entered.",
            kind="sign_in_resumed",
            shown=shown,
        ),
        "",
    )


def _on_this_page(
    asked_for: list[CredentialField], page: dict[str, Any], host: str
) -> tuple[CredentialField, ...] | None:
    """The request's fields, pointed at this page's elements — or `None` if this page
    is not that form any more.

    The request was read off the page by an earlier run, and element numbers are
    reassigned on every look. So the form is read again, here, and each field the
    request filled is matched to the field of the same kind on the page now. The key
    stays the request's: it is what the person's typed values are filed under.
    """
    if site_of(host_of(str(page.get("url", "")))) != host:
        return None
    current = form_fields(list(page.get("elements", [])))
    out: list[CredentialField] = []
    taken: set[str] = set()
    for wanted in (f for f in asked_for if f.elements):
        match = next((c for c in current if c.kind == wanted.kind and c.key not in taken), None)
        if match is None:
            return None
        taken.add(match.key)
        out.append(CredentialField(wanted.key, wanted.kind, wanted.label, match.elements))
    return tuple(out) or None
