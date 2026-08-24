"""The session summarizer node (PR-18).

Every LLM actor's graph ends with this node. It runs a `SUMMARIZATION`-class model
call when the session has drifted `SUMMARIZE_EVERY` messages past its last summary,
and does nothing otherwise.

**Why it is a shared node rather than a fifth actor.** PR-18 says "summarizer
worker". A separate actor would have been a cleaner reading of that phrase and a
worse system: it would need its own spec, its own runs, its own place in the
dispatcher, and it would put a run boundary between "the actor finished" and "the
actor's history was compressed" — which is a window in which the next run reads a
stale summary. Scope also says four actors, and adding a fifth to serve the
plumbing would make the coordination ratio measure the wrong department. So the
call lives here, is tagged SUMMARIZATION, and shows up in the ratio as overhead,
which is exactly what it is.

**Why the model call is here and the persistence is in `runtime.org`.** A model call
needs `ModelGateway`, and `runtime.org` sits *below* the gateway in the layer
contract. The split is not bureaucratic: the summary text can be tested without a
provider, and the gateway accounting can be tested without a session.

**Idempotency has a cost consequence.** `record_summary()` conflicts on
`(session_id, upto_message_count)`, so a replayed node writes nothing. But the model
call has already happened by then, so the node checks the cut point *before*
calling. A replay that skipped the check would pay for a summary it then discards,
and that spend lands in the coordination ratio.
"""

from __future__ import annotations

from typing import Any

from runtime.domain.context import RunContext
from runtime.domain.enums import WorkClass
from runtime.gateway.models import ModelGateway, ModelRequest
from runtime.observability.logging import get_logger
from runtime.org.sessions import SessionService, SessionView
from runtime.persistence.repositories.inbox import InboxRow

log = get_logger("graphs.summarize")

SYSTEM = (
    "You compress an agent's working history so its next session starts informed "
    "rather than blank. Write for the agent, not for a human reader."
)

INSTRUCTION = (
    "Summarise the exchange below in at most 200 words.\n\n"
    "Keep: decisions taken and why, commitments made, what was rejected and on what "
    "grounds, and anything still open.\n"
    "Drop: pleasantries, restatements of the task, and anything the agent can "
    "re-derive from its inputs.\n\n"
    "Write plain prose. No preamble, no headings, no bullet list."
)

MAX_SUMMARY_TOKENS = 600


async def maybe_summarize(
    ctx: RunContext,
    models: ModelGateway,
    sessions: SessionService,
    session: SessionView,
    messages: list[InboxRow],
    *,
    previous_summary: str | None = None,
) -> dict[str, Any]:
    """Summarise if the session has drifted far enough. Returns what it did.

    Returns rather than raises on every failure path, because a failed summary must
    not fail a run that has already produced an accepted artifact. Losing a summary
    costs the next run some context; losing the run costs the week.
    """
    if not session.needs_summary:
        return {"summarized": False, "reason": "not due", "cost_cents": 0}
    if not messages:
        return {"summarized": False, "reason": "no messages", "cost_cents": 0}

    cut = session.message_count
    transcript = "\n".join(
        f"[{m.created_at:%Y-%m-%d %H:%M}] {m.sender_name or 'system'} → "
        f"{m.recipient_name} ({m.kind}): {m.subject}"
        for m in messages
    )
    carried = (
        f"Previous summary, which this one supersedes:\n{previous_summary}\n\n"
        if previous_summary
        else ""
    )

    with ctx.node("summarize"):
        response = await models.complete(
            ctx,
            ModelRequest(
                prompt=f"{carried}{INSTRUCTION}\n\n## Exchange\n{transcript}",
                system=SYSTEM,
                max_output_tokens=MAX_SUMMARY_TOKENS,
            ),
            work_class=WorkClass.SUMMARIZATION,
            call_site="summarize.session",
        )

    text = response.text.strip()
    if not text:
        log.warning("summarize.empty", session_id=str(session.session_id), **ctx.log_fields())
        return {"summarized": False, "reason": "empty response", "cost_cents": response.cost_cents}

    written = await sessions.record_summary(
        session.session_id,
        upto_message_count=cut,
        summary=text,
        run_id=ctx.run_id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_cents=response.cost_cents,
    )
    return {
        "summarized": written,
        "reason": None if written else "cut point already summarized",
        "cost_cents": response.cost_cents,
        "upto": cut,
    }
