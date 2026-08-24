"""M3 through the real department, not through the planner.

Every other M3 test drives `ContextPlanner` directly. This one runs the actual weekly
loop with a memory subsystem attached to the real `Worker`, and asserts on the prompt
the *provider* received — which is the only place the question "did a memory reach a
model" can be answered honestly.

**The node under test is `marketing-head`'s Monday plan**, not `research.synthesize`.
Two reasons, and the first is practical: `web.search@1` refuses rather than degrades
when `RUNTIME_SEARCH_ENDPOINT_URL` is unset (M1's deliberate choice), so the research
run fails before its model call on any machine without a search endpoint — which is why
`test_m1_actors.py` already guards its `research.synthesize` assertion with an `if`.
Building an M3 test on a node that does not run everywhere would make it pass by being
skipped.

The second reason is better: the plan node is where §1's hypotheses actually live. All
three are about the head re-deriving the same thing every Monday, so it is the node whose
prompt matters most, and the one whose retrieval is worth watching when the injection
week is measured.

It exists because everything between the planner and the model is wiring, and wiring is
what breaks quietly: `NodeContext.recall` returning an empty plan because `memory` was
never attached, `assemble(memory=...)` dropping the section, a node passing a query built
from a field that is empty in practice. None of those would fail a single test above.

Both directions are checked. **Shadow mode: no prompt contains a memory.** **Injection
on: the prompt does, and only for the actors named.** Same organization, same memories,
same loop — the only difference is the flag, which is §9's build-order guarantee arriving
at the one place it is finally observable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import MemoryScope
from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org
from tests.conftest_m3 import build_memory_harness

pytestmark = pytest.mark.integration

MON_0800 = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)

FACT = (
    "category position",
    "the agent-infrastructure category rewards compliance claims over speed claims",
)
"""Written to be retrievable *by the plan node's query*, which is built from the goal
statement and the project name — see `marketing_head._plan`. A fact phrased so it shares
no tokens with that query would make the injection test fail for a reason that has
nothing to do with the wiring, since `HashingEmbedder` does lexical similarity only."""


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[Any]:
    async for runtime in build_m1(settings, new_org()):
        yield runtime


async def _attach(m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings, **kw: Any) -> Any:
    """Give the real worker a memory subsystem, then seed one fact into the head's scope.

    The subsystem is built by the same helper the other M3 tests use, and attached with
    `Worker.attach_memory` — the same call `worker.main` makes. Nothing here reaches
    around the wiring, which is the whole point of the file.
    """
    harness = await build_memory_harness(uow_factory, settings, **kw)
    m1.worker.attach_memory(_Subsystem(harness))

    async with uow_factory() as uow:
        actor_id = (
            await uow.session.execute(
                text(
                    "SELECT id FROM actors WHERE organization_id = :o AND name = 'marketing-head'"
                ),
                {"o": m1.organization_id},
            )
        ).scalar_one()

    await harness.write(
        OrganizationId(m1.organization_id),
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=uuid.UUID(str(actor_id)),
        facts=[FACT],
        actor_id=uuid.UUID(str(actor_id)),
    )
    return harness


class _Subsystem:
    """The two attributes `Worker.attach_memory` reads. Not a `MemorySubsystem`.

    Constructing a real one would build a second `ModelGateway` and a second embedding
    cache, and the test would then be measuring which of two subsystems the worker
    happened to use. This carries the harness's planner and nothing else.
    """

    def __init__(self, harness: Any) -> None:
        self.planner = harness.planner
        self.service = harness.service


def _plan_prompt(m1: Any) -> str | None:
    for call in m1.provider.calls:
        if call.call_site == "head.weekly_plan":
            return call.prompt
    return None


async def _run_monday(m1: Any) -> None:
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=8)


async def test_shadow_mode_puts_no_memory_in_a_real_prompt(
    m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§5's guarantee, observed where it matters: at the provider."""
    await _attach(m1, uow_factory, settings)
    await _run_monday(m1)

    prompt = _plan_prompt(m1)
    assert prompt is not None, "the Monday plan never reached its model call"
    assert "What you already know" not in prompt
    assert "compliance claims over speed" not in prompt


async def test_injection_puts_the_memory_in_the_real_prompt(
    m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The other half. Without this, the test above passes on a system where retrieval
    is simply broken — which is the failure mode a one-sided shadow test cannot see."""
    await _attach(m1, uow_factory, settings, injection=True)
    await _run_monday(m1)

    prompt = _plan_prompt(m1)
    assert prompt is not None
    assert "What you already know" in prompt, (
        "memory was enabled and injected, and nothing reached the prompt — the wiring "
        "between ContextPlanner and assemble() is broken"
    )
    assert "compliance claims over speed" in prompt


async def test_the_per_actor_flag_reaches_the_real_graphs(
    m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§13 risk 1's escape hatch, end to end: `content` is named, so `marketing-head`
    stays in shadow mode even though the subsystem is live and has a matching memory."""
    await _attach(m1, uow_factory, settings, injection=True, actors="content")
    await _run_monday(m1)

    prompt = _plan_prompt(m1)
    assert prompt is not None
    assert "What you already know" not in prompt, (
        "injection was enabled for `content` only and `marketing-head` got a memory "
        "anyway — the per-actor flag is not reaching the graphs"
    )


async def test_a_real_run_writes_a_context_trace(
    m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """T54 through the loop rather than through the planner: the trace carries the run
    id, the actor and the node a person will actually search on."""
    await _attach(m1, uow_factory, settings)
    await _run_monday(m1)

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT actor_name, node, call_site, shadow_mode, retrieved_count, "
                    "query_text FROM context_traces WHERE organization_id = :o"
                ),
                {"o": m1.organization_id},
            )
        ).all()

    by_actor = {r.actor_name for r in rows}
    assert "marketing-head" in by_actor, f"no head trace; got {by_actor}"
    head = next(r for r in rows if r.actor_name == "marketing-head")
    assert head.node == "plan"
    assert head.call_site == "head.weekly_plan"
    assert head.shadow_mode is True
    assert head.query_text, (
        "the node built an empty retrieval query — retrieval would match on recency "
        "alone, which is the quietest way for eval 2 to be bad"
    )


async def test_memory_off_leaves_the_loop_exactly_as_m2_had_it(
    m1: Any, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """No subsystem attached at all — `NodeContext.memory` is `None`.

    The M1 and M2 suites already run this way; what this adds is the assertion that the
    *absence* is silent: no trace, no embedding call, and a prompt with no memory
    section. A checkout with M3 merged and the flag off has to be indistinguishable from
    one without it, or the earlier measurement weeks cannot be re-run from this tree.
    """
    await _run_monday(m1)

    prompt = _plan_prompt(m1)
    assert prompt is not None
    assert "What you already know" not in prompt

    async with uow_factory() as uow:
        traces = (
            await uow.session.execute(
                text("SELECT count(*) FROM context_traces WHERE organization_id = :o"),
                {"o": m1.organization_id},
            )
        ).scalar_one()
        embeddings = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE organization_id = :o "
                    "AND gateway = 'embedding'"
                ),
                {"o": m1.organization_id},
            )
        ).scalar_one()
    assert traces == 0
    assert embeddings == 0
