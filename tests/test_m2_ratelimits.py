"""M2 §7 — rate limits. Redis token buckets, policy in Postgres.

Not numbered in §8's table, but §7 requires all three scopes and says why: *"per
connection, per provider, per actor — all three, because they fail differently."* The
tests are that sentence, plus the two properties that are easy to get wrong: the
bucket must be atomic under concurrency, and a Redis outage must apply the policy's
own fail mode rather than a global guess.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from redis.asyncio import Redis

from runtime.domain.errors import RateLimited
from runtime.domain.ids import OrganizationId
from runtime.gateway.ratelimit import RateLimiter
from runtime.gateway.tools import ToolCall
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m2 import (
    build_harness,
    grant_tools,
    make_authority,
    make_ctx,
    noop_tool_def,
)

pytestmark = pytest.mark.integration


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    await grant_tools(uow_factory, organization_id, "research", "test.noop@1")
    return organization_id


async def _policy(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    *,
    scope_type: str,
    scope_id: str,
    limit: int,
    window: int = 60,
    fail_open: bool = True,
) -> None:
    async with uow_factory.transaction() as uow:
        await uow.rate_limits.upsert(
            uuid.uuid4(),
            organization_id,
            scope_type=scope_type,
            scope_id=scope_id,
            limit_per_window=limit,
            window_seconds=window,
            fail_open=fail_open,
        )


@pytest.fixture
async def redis(settings: Settings):  # type: ignore[no-untyped-def]
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_a_bucket_admits_its_burst_and_then_refuses(
    uow_factory: UnitOfWorkFactory, redis: Redis
) -> None:
    organization_id = await _org(uow_factory)
    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=3)
    limiter = RateLimiter(uow_factory, redis)

    verdicts = [await limiter.check(organization_id, actor="research") for _ in range(5)]

    assert [v.allowed for v in verdicts] == [True, True, True, False, False]
    assert verdicts[-1].scope == "actor:research"
    assert verdicts[-1].retry_after_s > 0, "a limiter that cannot say when is not usable"


async def test_the_bucket_is_atomic_under_concurrency(
    uow_factory: UnitOfWorkFactory, redis: Redis
) -> None:
    """The reason it is a Lua script.

    "Read tokens, decide, write tokens" across a network admits more than its limit
    under any concurrency, which is the one thing a limiter must not do. Twenty
    concurrent takes against a bucket of five must admit exactly five.
    """
    organization_id = await _org(uow_factory)
    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=5)
    limiter = RateLimiter(uow_factory, redis)

    verdicts = await asyncio.gather(
        *(limiter.check(organization_id, actor="research") for _ in range(20))
    )
    assert sum(v.allowed for v in verdicts) == 5


async def test_all_three_scopes_are_enforced(uow_factory: UnitOfWorkFactory, redis: Redis) -> None:
    """§7: they fail differently, so a call inside its actor limit can still be
    stopped by the provider-wide one."""
    organization_id = await _org(uow_factory)
    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=50)
    await _policy(uow_factory, organization_id, scope_type="provider", scope_id="serper", limit=2)
    await _policy(
        uow_factory, organization_id, scope_type="connection", scope_id="search-primary", limit=50
    )
    limiter = RateLimiter(uow_factory, redis)

    calls = [
        await limiter.check(
            organization_id, actor="research", provider="serper", connection="search-primary"
        )
        for _ in range(3)
    ]
    assert [c.allowed for c in calls] == [True, True, False]
    assert calls[-1].scope == "provider:serper", "the provider aggregate, not the actor"


async def test_a_scope_with_no_policy_is_unlimited(
    uow_factory: UnitOfWorkFactory, redis: Redis
) -> None:
    """Absence of a policy is not a limit of zero. A default-deny rate limiter would
    stop the whole organization the moment somebody forgot a row."""
    organization_id = await _org(uow_factory)
    limiter = RateLimiter(uow_factory, redis)
    for _ in range(50):
        assert (await limiter.check(organization_id, actor="research")).allowed


async def test_redis_being_down_applies_the_policys_own_fail_mode(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Both answers are defensible and the right one differs per limit, which is why
    it is data rather than a setting.

    An actor loop guard fails open — the budget still bounds it, and stopping the
    department to protect a guard is worse. A connection whose provider bans on abuse
    fails closed, and accepts that a Redis outage stops those calls.
    """
    organization_id = await _org(uow_factory)
    await _policy(
        uow_factory,
        organization_id,
        scope_type="actor",
        scope_id="research",
        limit=5,
        fail_open=True,
    )
    await _policy(
        uow_factory,
        organization_id,
        scope_type="connection",
        scope_id="search-primary",
        limit=5,
        fail_open=False,
    )
    offline = RateLimiter(uow_factory, None)

    assert (await offline.check(organization_id, actor="research")).allowed
    refused = await offline.check(organization_id, connection="search-primary")
    assert not refused.allowed
    assert "fail_closed" in refused.reason


async def test_a_rate_limited_tool_call_gives_its_budget_hold_back(
    uow_factory: UnitOfWorkFactory, settings: Settings, redis: Redis
) -> None:
    """The pipeline puts the rate limit *after* the reservation, per the documented
    order — so a refusal has to release. Without it every throttled call shrinks the
    pool until the sweeper catches up, which is a budget leak wearing a rate limiter's
    clothes.
    """
    from runtime.budget.service import BudgetService
    from runtime.domain.ids import ActorId

    organization_id = await _org(uow_factory)
    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=1)

    async with uow_factory.transaction() as uow:
        leaf = await BudgetService().ensure_chain(
            uow, organization_id, actor_id=ActorId(uuid.uuid4()), department="marketing"
        )

    harness = build_harness(
        uow_factory,
        settings,
        tool_def=noop_tool_def(provider="serper"),
        rate_limiter=RateLimiter(uow_factory, redis),
    )
    ctx = make_ctx(organization_id, authority=make_authority(), budget_pool_id=str(leaf))

    await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "one"}))
    with pytest.raises(RateLimited):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "two"}))

    async with uow_factory() as uow:
        chain = await uow.budget.chain(leaf)
        decisions = await uow.audit.decisions_for_run(ctx.run_id)
    assert all(p.reserved_cents == 0 for p in chain), "the throttled call held nothing"
    assert any(d["check"] == "rate_limit" for d in decisions), "and it is in the denial stream"


async def test_a_flushed_redis_costs_one_permissive_window_and_nothing_else(
    uow_factory: UnitOfWorkFactory, redis: Redis
) -> None:
    """I10 holds: nothing in Redis needs rebuilding from Postgres, because the policy
    — the durable half — is already there. A FLUSHALL resets the counters and the
    limit still applies from the next window."""
    organization_id = await _org(uow_factory)
    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=2)
    limiter = RateLimiter(uow_factory, redis)

    for _ in range(2):
        assert (await limiter.check(organization_id, actor="research")).allowed
    assert not (await limiter.check(organization_id, actor="research")).allowed

    await redis.flushdb()

    assert (await limiter.check(organization_id, actor="research")).allowed, (
        "one permissive window is the whole cost of losing the counters"
    )


async def test_the_policy_cache_is_bounded(uow_factory: UnitOfWorkFactory, redis: Redis) -> None:
    """A policy read per tool call is a round trip §9 would notice; thirty seconds is
    fast enough for a per-minute quota."""
    organization_id = await _org(uow_factory)
    limiter = RateLimiter(uow_factory, redis, ttl_seconds=300.0)
    assert (await limiter.check(organization_id, actor="research")).allowed  # no policy yet

    await _policy(uow_factory, organization_id, scope_type="actor", scope_id="research", limit=1)
    assert (await limiter.check(organization_id, actor="research")).allowed, "still cached"

    limiter.invalidate(organization_id)
    assert (await limiter.check(organization_id, actor="research")).allowed
    assert not (await limiter.check(organization_id, actor="research")).allowed


async def test_a_burst_below_the_window_limit_is_refused_by_the_database(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A bucket that cannot hold one window's worth of tokens is a limit that silently
    under-delivers. The CHECK constraint is what stops it being configurable."""
    from sqlalchemy.exc import IntegrityError

    organization_id = await _org(uow_factory)
    with pytest.raises(IntegrityError):
        async with uow_factory.transaction() as uow:
            await uow.rate_limits.upsert(
                uuid.uuid4(),
                organization_id,
                scope_type="actor",
                scope_id="research",
                limit_per_window=10,
                window_seconds=60,
                burst=1,
            )
