"""Rate limits — Redis token buckets, policy in Postgres.

Three scopes, all three enforced, because they fail differently:

- **connection** — the provider's real limit for one credential. Exceeding it gets the
  key throttled or banned, and the blast radius is every run sharing it.
- **provider** — the aggregate across connections. Two connections each within their
  own limit can still put a provider-wide quota over.
- **actor** — a runaway-loop guard independent of budget. A free tool called ten
  thousand times costs nothing and is still an incident.

**The bucket is one Lua script**, and it has to be, because "read tokens, decide,
write tokens" across a network is a race with a name: under any concurrency the
limiter admits more than its limit, which is the one thing a limiter must not do.
Redis runs the script atomically, so the decision and the debit are the same event.

**Redis holding the counters respects I10.** A `FLUSHALL` loses the current window's
consumption and nothing else — at worst one window runs permissive, and the policy
that defines the limit is in Postgres where it can be rebuilt from. Nothing here is
state that cannot be reconstructed; it is state that does not need to be.

**`fail_open` is per policy, and both answers are defensible.** For an actor loop
guard, failing open during a Redis outage is right: the budget still bounds it, and
stopping the organization to protect a guard is worse than the guard being off for
five minutes. For a connection whose provider bans on abuse it is wrong, and that
policy sets `fail_open = false` and accepts that a Redis outage stops those calls. The
choice is data because the right answer differs per limit, and a global setting would
force one of the two wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from runtime.domain.errors import RateLimited
from runtime.domain.ids import OrganizationId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.ratelimits import RateLimitPolicy
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("gateway.ratelimit")

POLICY_CACHE_TTL_SECONDS = 30.0
"""Same bound as the permission cache, same reasoning: a policy read per tool call is
a round trip §9 would notice, and thirty seconds is fast enough for a per-minute
quota."""

_BUCKET_SCRIPT = """
-- Token bucket. KEYS[1] = bucket key.
-- ARGV: 1 now (float seconds), 2 refill/sec, 3 burst, 4 cost, 5 ttl seconds.
local now    = tonumber(ARGV[1])
local rate   = tonumber(ARGV[2])
local burst  = tonumber(ARGV[3])
local cost   = tonumber(ARGV[4])
local ttl    = tonumber(ARGV[5])

local state  = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(state[1])
local at     = tonumber(state[2])

if tokens == nil then
  tokens = burst
  at = now
end

-- Refill for elapsed time, capped at burst. `max(0, ...)` guards a clock that went
-- backwards: without it a backwards jump credits negative tokens and the bucket
-- locks out until it catches up.
local elapsed = math.max(0, now - at)
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'at', now)
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, tostring(tokens), tostring(retry_after)}
"""


@dataclass(frozen=True, slots=True)
class RateVerdict:
    allowed: bool
    scope: str = ""
    remaining: float = 0.0
    retry_after_s: float = 0.0
    reason: str = ""

    def raise_if_limited(self, what: str) -> None:
        if not self.allowed:
            raise RateLimited(
                f"{what} is rate limited by {self.scope}: retry in "
                f"{self.retry_after_s:.1f}s ({self.reason})"
            )


ALLOWED = RateVerdict(allowed=True)


@dataclass(frozen=True, slots=True)
class _PolicyCache:
    policies: tuple[RateLimitPolicy, ...]
    expires_at: float


class RateLimiter:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        redis: Redis | None,
        *,
        ttl_seconds: float = POLICY_CACHE_TTL_SECONDS,
        key_prefix: str = "runtime:rl",
    ) -> None:
        self._uow = uow_factory
        self._redis = redis
        self._ttl = ttl_seconds
        self._prefix = key_prefix
        self._cache: dict[str, _PolicyCache] = {}
        self._script: object | None = None

    async def policies(self, organization_id: OrganizationId) -> tuple[RateLimitPolicy, ...]:
        key = str(organization_id)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit.expires_at > now:
            return hit.policies
        async with self._uow() as uow:
            rows = tuple(await uow.rate_limits.policies(organization_id))
        self._cache[key] = _PolicyCache(rows, now + self._ttl)
        return rows

    def invalidate(self, organization_id: OrganizationId | None = None) -> None:
        if organization_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(organization_id), None)

    async def check(
        self,
        organization_id: OrganizationId,
        *,
        actor: str | None = None,
        provider: str | None = None,
        connection: str | None = None,
        cost: int = 1,
    ) -> RateVerdict:
        """Take a token from every applicable bucket. Refused by the first empty one.

        Buckets are debited in the order they are checked, so a call refused by the
        third policy has already spent tokens in the first two. That is a deliberate
        simplification and it is worth naming: making it atomic across three buckets
        would need a single script over all three keys, which cannot be done safely
        across a Redis cluster's hash slots. The cost of the simplification is that a
        heavily rate-limited call consumes a little quota it did not use — which
        throttles *harder* under contention, not softer, so it errs in the safe
        direction.
        """
        wanted = {"actor": actor, "provider": provider, "connection": connection}
        applicable = [
            p
            for p in await self.policies(organization_id)
            if wanted.get(p.scope_type) is not None and wanted[p.scope_type] == p.scope_id
        ]
        if not applicable:
            return ALLOWED

        for policy in applicable:
            verdict = await self._take(organization_id, policy, cost)
            if not verdict.allowed:
                return verdict
        return ALLOWED

    async def _take(
        self, organization_id: OrganizationId, policy: RateLimitPolicy, cost: int
    ) -> RateVerdict:
        if self._redis is None:
            return self._unavailable(policy, "no redis configured")
        key = f"{self._prefix}:{organization_id}:{policy.key}"
        try:
            if self._script is None:
                self._script = self._redis.register_script(_BUCKET_SCRIPT)
            allowed, tokens, retry_after = await self._script(  # type: ignore[operator]
                keys=[key],
                args=[
                    time.time(),
                    policy.refill_per_second,
                    policy.burst,
                    cost,
                    # Two windows of idle time before the key is dropped. A shorter
                    # TTL would let a bucket expire and reset to full mid-window,
                    # which is a limit that quietly does not apply.
                    int(policy.window_seconds * 2) + 1,
                ],
            )
        except RedisError as exc:
            return self._unavailable(policy, f"{type(exc).__name__}: {exc}")

        if int(allowed) == 1:
            return RateVerdict(allowed=True, scope=policy.key, remaining=float(tokens))
        return RateVerdict(
            allowed=False,
            scope=policy.key,
            remaining=float(tokens),
            retry_after_s=float(retry_after),
            reason=f"{policy.limit_per_window} per {policy.window_seconds}s",
        )

    def _unavailable(self, policy: RateLimitPolicy, why: str) -> RateVerdict:
        """Redis is not answering. Apply the policy's own fail mode, and say so loudly.

        The log line is `warning` in both directions on purpose. Failing open is a
        decision to run unprotected and failing closed is a decision to stop working;
        neither should pass unremarked just because it was configured in advance.
        """
        log.warning(
            "ratelimit.unavailable",
            scope=policy.key,
            fail_open=policy.fail_open,
            reason=why,
        )
        if policy.fail_open:
            return RateVerdict(allowed=True, scope=policy.key, reason=f"fail_open: {why}")
        return RateVerdict(
            allowed=False,
            scope=policy.key,
            retry_after_s=1.0,
            reason=f"fail_closed: {why}",
        )
