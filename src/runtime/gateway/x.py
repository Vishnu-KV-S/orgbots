"""The X API (v2) — reading an account's mentions and replying to one.

Three calls, nothing else:

- `user(handle)` — `GET /2/users/by/username/:handle`, to find the account's id when an
  admin connects it (and to prove the token works);
- `mentions(user_id, since_id)` — `GET /2/users/:id/mentions`, newest first, with the
  posts they reply to and quote expanded, their authors, and media types;
- `reply(text, to)` — `POST /2/tweets` as the account, which needs a user-context token
  (an app-only token can read but not post).

Tokens arrive opened from the vault and are used for the one request. X's rate limits
answer 429; that is an `XAPIError` like any other, and the poller tries again on its
next round.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from runtime.domain.x import Post

API = "https://api.x.com/2"
TIMEOUT_S = 20.0


class XAPIError(Exception):
    """X refused or could not be reached. Shown to admins."""


def _post(raw: dict[str, Any], users: dict[str, str], media: dict[str, str]) -> Post:
    created = raw.get("created_at")
    keys = (raw.get("attachments") or {}).get("media_keys") or []
    return Post(
        id=str(raw["id"]),
        author_id=str(raw.get("author_id", "")),
        author_handle=users.get(str(raw.get("author_id", "")), ""),
        text=str(raw.get("note_tweet", {}).get("text") or raw.get("text") or ""),
        created_at=dt.datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
        media=tuple(media[k] for k in keys if k in media),
    )


class Mention:
    """A post that mentions the account, with what it points at."""

    def __init__(self, post: Post, parent: Post | None, quoted: list[Post]) -> None:
        self.post, self.parent, self.quoted = post, parent, quoted


class XClient:
    def __init__(
        self,
        token: str,
        *,
        base: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self._transport = transport

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_S,
                transport=self._transport,
                headers={"authorization": f"Bearer {self._token}"},
            ) as http:
                got = await http.request(method, f"{self._base}{path}", **kw)
        except httpx.HTTPError as exc:
            raise XAPIError(f"X is not reachable ({type(exc).__name__})") from exc
        try:
            body = got.json()
        except ValueError:
            body = {}
        if got.status_code >= 300:
            said = body.get("detail") or body.get("title") or got.text[:200]
            raise XAPIError(f"X answered {got.status_code}: {said}")
        return body if isinstance(body, dict) else {}

    async def user(self, handle: str) -> tuple[str, str]:
        """`(id, username)` for a handle."""
        body = await self._call("GET", f"/users/by/username/{handle}")
        data = body.get("data")
        if not isinstance(data, dict) or "id" not in data:
            raise XAPIError(f"X has no account @{handle}")
        return str(data["id"]), str(data.get("username", handle))

    async def mentions(self, user_id: str, since_id: str | None) -> list[Mention]:
        params: dict[str, Any] = {
            "max_results": 50,
            "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id,"
            "attachments.media_keys",
            "tweet.fields": "created_at,text,author_id,referenced_tweets,attachments,note_tweet",
            "user.fields": "username",
            "media.fields": "type",
        }
        if since_id:
            params["since_id"] = since_id
        body = await self._call("GET", f"/users/{user_id}/mentions", params=params)
        includes = body.get("includes") or {}
        users = {str(u["id"]): str(u.get("username", "")) for u in includes.get("users", [])}
        media = {str(m["media_key"]): str(m.get("type", "")) for m in includes.get("media", [])}
        referenced = {
            str(t["id"]): _post(t, users, media) for t in includes.get("tweets", []) if "id" in t
        }
        out: list[Mention] = []
        for raw in body.get("data") or []:
            post = _post(raw, users, media)
            parent: Post | None = None
            quoted: list[Post] = []
            for ref in raw.get("referenced_tweets") or []:
                found = referenced.get(str(ref.get("id")))
                if found is None:
                    continue
                if ref.get("type") == "replied_to":
                    parent = found
                elif ref.get("type") == "quoted":
                    quoted.append(found)
            out.append(Mention(post, parent, quoted))
        return out

    async def reply(self, text: str, to: str) -> str:
        body = await self._call(
            "POST", "/tweets", json={"text": text, "reply": {"in_reply_to_tweet_id": to}}
        )
        return str((body.get("data") or {}).get("id", ""))
