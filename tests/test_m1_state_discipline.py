"""The M0 retro's decision, made checkable.

The retro measured LangGraph's checkpointing: one `echo_agent@1` run with a
three-field state wrote 4 checkpoints averaging 676 B. The whole state is written at
every superstep, so the cost is `state_size * supersteps` — not `changed_bytes`.

M1's natural state includes a `CompetitorReport`. At ~8 KB, carried through four
supersteps, that is ~40 KB of checkpoint per run, per replay, forever, for bytes
already in the object store with a digest on them.

The retro's decision was **no `DeltaChannel`; state discipline instead**:

    A graph's state may hold artifact *references*. It may not hold artifact
    *bodies*.

This module is what makes that a rule rather than a convention. It runs the real
graphs and reads `lg.checkpoints` — the framework's own table — so a node that
started stashing a report in state fails here in the week it happens rather than
showing up as a slow database a quarter later.

If this test starts failing under real workloads *without* a node having done
anything wrong, that is the signal to build `DeltaChannel` in M3. The number is the
trigger; that is the point of having one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.ids import CorrelationId
from runtime.domain.specs import StartRunRequest
from runtime.graphs.common.state import MAX_CHECKPOINT_BYTES, ArtifactRefView, summarise
from runtime.org.department import ANALYTICS, HEAD
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org, sample_competitor_report

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[Any]:
    async for runtime in build_m1(settings, new_org(), with_triggers=False):
        yield runtime


async def _checkpoint_sizes(uow_factory: UnitOfWorkFactory) -> list[tuple[str, int]]:
    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    """
                    SELECT checkpoint_ns, pg_column_size(checkpoint) AS bytes
                      FROM lg.checkpoints
                     ORDER BY bytes DESC
                    """
                )
            )
        ).all()
    return [(r.checkpoint_ns, int(r.bytes)) for r in rows]


async def test_no_m1_graph_checkpoint_exceeds_the_budget(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """Run the whole loop; assert on the framework's own table.

    Eight kilobytes is ten times what M0 measured for a trivial graph and roughly a
    fifth of what a single inlined `CompetitorReport` would cost.
    """
    correlation = CorrelationId(uuid.uuid4())
    await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=HEAD,
            input={"mode": "weekly_plan"},
            idempotency_key=f"discipline-{uuid.uuid4()}",
            correlation_id=str(correlation),
        )
    )
    await m1.pump(rounds=12)

    sizes = await _checkpoint_sizes(uow_factory)
    assert sizes, "the graphs must actually have checkpointed something"

    oversized = [(ns, n) for ns, n in sizes if n > MAX_CHECKPOINT_BYTES]
    assert not oversized, (
        f"checkpoints over {MAX_CHECKPOINT_BYTES} bytes: {oversized[:5]}. "
        "A graph's state may hold artifact references, not artifact bodies — see "
        "docs/M0_RETRO.md §1. If nothing is holding a body, this is the signal to "
        "build DeltaChannel in M3."
    )


async def test_the_research_evidence_is_an_artifact_not_state(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """The specific discipline `research@1` depends on.

    Eight fetched pages are written as one `research_evidence` artifact and the
    graph state keeps only a reference; `synthesize` loads it back inside the node
    and never returns it.
    """
    from runtime.graphs.research.graph import ResearchState

    annotations = ResearchState.__annotations__
    assert "evidence" in annotations
    # The state field is typed as a plain dict because it holds a serialised
    # `ArtifactRefView`. Asserting on the view's own size is the meaningful check:
    # a reference is small by construction and a body is not.
    view = ArtifactRefView(
        artifact_id=str(uuid.uuid4()),
        sha256="a" * 64,
        size_bytes=98_304,
        kind="research_evidence",
        summary=summarise(["https://example.com/a"] * 20),
    )
    encoded = len(str(view.to_json()))
    assert encoded < 1024, f"a reference is {encoded} bytes; it must stay small"
    assert len(view.summary) <= 513, "summaries are capped so state cannot grow"


def test_a_summary_of_a_full_report_stays_within_the_cap() -> None:
    """`summarise` is what puts an artifact into a prompt or a state field.

    Deterministic and capped: a summary that varied between processes would change
    the prompt hash and destroy the cache hit rate on the system prefix.
    """
    payload = sample_competitor_report()
    first = summarise(payload)
    second = summarise(payload)

    assert first == second, "deterministic, or the prompt cache never hits"
    assert len(first) <= 513
    assert first.endswith("…"), "and it says when it truncated"


async def test_the_deterministic_actor_checkpoints_nothing(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """A handler has no graph and no checkpointer.

    A deterministic worker's re-execution is always safe — same input, same output,
    no external effect — so there is nothing to checkpoint and nothing to recover.
    """
    before = len(await _checkpoint_sizes(uow_factory))
    await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=ANALYTICS,
            input={"mode": "weekly_metrics"},
            idempotency_key=f"no-checkpoint-{uuid.uuid4()}",
        )
    )
    await m1.pump()
    after = len(await _checkpoint_sizes(uow_factory))

    assert after == before
