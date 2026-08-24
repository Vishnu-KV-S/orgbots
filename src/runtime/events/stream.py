"""Redis Streams transport.

Redis is an accelerator here, not a source of truth. Postgres holds the outbox and
the runs; the stream exists so a worker learns about a run in milliseconds instead
of at the next poll. Everything in this module is written on the assumption that
Redis can be wiped at any moment (R1/T8) and the system must lose nothing.

The one non-obvious piece is `_PUBLISH_LUA`. The relay is at-least-once — it can
die between `XADD` and marking the outbox row published — so without a dedupe the
stream would grow a second copy of an event on every relay restart. Doing the
dedupe check and the `XADD` as two round trips just moves the race. One Lua script
makes them atomic.
"""

from __future__ import annotations

from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from runtime.settings import Settings, get_settings

_PUBLISH_LUA: Final = """
-- KEYS[1] stream, KEYS[2] dedupe hash
-- ARGV[1] maxlen, ARGV[2] dedupe field, ARGV[3..] field/value pairs
if redis.call('HSETNX', KEYS[2], ARGV[2], 1) == 0 then
  return false
end
local args = {'XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], '*'}
for i = 3, #ARGV do
  table.insert(args, ARGV[i])
end
return redis.call(unpack(args))
"""

DEDUPE_TTL_SECONDS = 24 * 3600
"""The dedupe hash is a cache, not a ledger. If it expires, the worst case is a
duplicate stream entry, and every consumer of the stream is idempotent on
`run_id` anyway — the outbox row's `published_at` is the durable record."""


class RedisStreams:
    def __init__(self, settings: Settings | None = None, client: Redis | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = client or Redis.from_url(self._settings.redis_url, decode_responses=True)
        self._publish_sha: str | None = None

    @property
    def client(self) -> Redis:
        return self._client

    def stream_key(self, topic: str) -> str:
        return f"{self._settings.stream_prefix}:{topic}"

    def dedupe_key(self, topic: str) -> str:
        return f"{self._settings.stream_prefix}:published:{topic}"

    async def _ensure_script(self) -> str:
        if self._publish_sha is None:
            self._publish_sha = await self._client.script_load(_PUBLISH_LUA)
        return self._publish_sha

    async def publish(self, topic: str, dedupe: str, fields: dict[str, str]) -> str | None:
        """`XADD` unless this dedupe key was already published.

        Returns the stream entry ID, or None if it was a duplicate. Reloads the
        script if Redis was flushed out from under us — after a `FLUSHALL` the
        cached SHA is stale, and treating that as a hard error would turn a
        recoverable wipe into an outage.
        """
        flat: list[str] = []
        for key, value in fields.items():
            flat.extend((key, value))
        args = [str(self._settings.stream_maxlen), dedupe, *flat]
        keys = [self.stream_key(topic), self.dedupe_key(topic)]

        try:
            result = await self._client.evalsha(await self._ensure_script(), 2, *keys, *args)
        except ResponseError as exc:
            if "NOSCRIPT" not in str(exc):
                raise
            self._publish_sha = None
            result = await self._client.evalsha(await self._ensure_script(), 2, *keys, *args)

        await self._client.expire(self.dedupe_key(topic), DEDUPE_TTL_SECONDS)
        if result is False or result is None:
            return None
        return str(result)

    async def ensure_group(self, topic: str, group: str | None = None) -> None:
        """Create the consumer group, tolerating "already exists".

        `mkstream=True` so a worker can subscribe before the first event is ever
        published, which is the normal order on a cold start.
        """
        try:
            await self._client.xgroup_create(
                self.stream_key(topic),
                group or self._settings.consumer_group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self,
        topic: str,
        consumer: str,
        *,
        group: str | None = None,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[tuple[str, dict[str, str]]]:
        entries = await self._client.xreadgroup(
            groupname=group or self._settings.consumer_group,
            consumername=consumer,
            streams={self.stream_key(topic): ">"},
            count=count,
            block=block_ms,
        )
        return _flatten(entries)

    async def read_pending(
        self,
        topic: str,
        consumer: str,
        *,
        group: str | None = None,
        count: int = 10,
    ) -> list[tuple[str, dict[str, str]]]:
        """Entries this consumer took and never acked — i.e. what it was holding
        when it died."""
        entries = await self._client.xreadgroup(
            groupname=group or self._settings.consumer_group,
            consumername=consumer,
            streams={self.stream_key(topic): "0"},
            count=count,
        )
        return _flatten(entries)

    async def claim_stale(
        self,
        topic: str,
        consumer: str,
        *,
        min_idle_ms: int,
        group: str | None = None,
        count: int = 10,
    ) -> list[tuple[str, dict[str, str]]]:
        """Take over entries another consumer has been sitting on.

        This recovers a dead worker's stream entries. It does *not* recover the
        run: the lease does that, and a stream entry for an already-claimed run is
        simply acked and dropped.
        """
        result = await self._client.xautoclaim(
            name=self.stream_key(topic),
            groupname=group or self._settings.consumer_group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            count=count,
        )
        # xautoclaim returns (next_cursor, entries, deleted) on Redis >= 7.
        entries = result[1] if len(result) > 1 else []
        return [(str(eid), _decode(fields)) for eid, fields in entries]

    async def ack(self, topic: str, entry_ids: list[str], group: str | None = None) -> None:
        if not entry_ids:
            return
        await self._client.xack(
            self.stream_key(topic), group or self._settings.consumer_group, *entry_ids
        )

    async def length(self, topic: str) -> int:
        return int(await self._client.xlen(self.stream_key(topic)))

    async def close(self) -> None:
        await self._client.aclose()


def _decode(fields: dict[Any, Any]) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else str(k)): (
            v.decode() if isinstance(v, bytes) else str(v)
        )
        for k, v in fields.items()
    }


def _flatten(entries: Any) -> list[tuple[str, dict[str, str]]]:
    out: list[tuple[str, dict[str, str]]] = []
    for _stream, messages in entries or []:
        for entry_id, fields in messages:
            eid = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
            out.append((eid, _decode(fields)))
    return out
