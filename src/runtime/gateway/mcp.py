"""A Model Context Protocol client — the Streamable HTTP transport, tools only.

A connector is an MCP server a person connected (`domain.connectors`): GitHub, Linear,
a company's own. This speaks just enough of the protocol to use one: `initialize`, the
`notifications/initialized` that follows it, `tools/list` (with its pagination), and
`tools/call`. Each request is a JSON-RPC 2.0 POST; the answer is either a JSON body or a
`text/event-stream` carrying the response as an event, and both are read here. A server
that hands out an `Mcp-Session-Id` gets it back on every later request of the session.

It lives in the gateway because it is an external call, and because the connector's
token is opened here and nowhere else — sent as the server asked (a bearer token, or a
named header) and dropped. The token never reaches a run's state, the model or the
computer.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "agent-org-runtime", "version": "0.1.0"}
TIMEOUT_S = 60.0
MAX_TOOLS = 200
MAX_RESULT_CHARS = 60_000


class MCPError(Exception):
    """A connector that could not be reached or answered with an error. Shown as is."""


@dataclass(frozen=True, slots=True)
class Auth:
    kind: str = "none"
    """`none`, `bearer` or `header`."""
    header: str = ""
    token: str = ""

    def headers(self) -> dict[str, str]:
        if self.kind == "bearer" and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.kind == "header" and self.header and self.token:
            return {self.header: self.token}
        return {}

    def __repr__(self) -> str:
        return f"Auth(kind={self.kind!r}, header={self.header!r})"


class MCPClient:
    """One session with one server. Use as an async context manager."""

    def __init__(
        self,
        url: str,
        auth: Auth | None = None,
        *,
        timeout_s: float = TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._auth = auth or Auth()
        self._timeout = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._ids = itertools.count(1)
        self.server: dict[str, Any] = {}

    async def __aenter__(self) -> MCPClient:
        self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            )
            self.server = dict(result.get("serverInfo") or {})
            await self._notify("notifications/initialized")
        except BaseException:
            await self._client.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(tools) < MAX_TOOLS:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools[:MAX_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    # --- the wire --------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self._auth.headers(),
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        try:
            response = await self._client.post(self._url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPError(f"could not reach the connector ({type(exc).__name__})") from exc
        session = response.headers.get("mcp-session-id")
        if session:
            self._session = session
        if response.status_code in (401, 403):
            raise MCPError(
                f"the connector refused the credentials ({response.status_code}); check the token"
            )
        if response.status_code >= 400:
            raise MCPError(f"the connector answered {response.status_code}: {_brief(response)}")
        return response

    async def _notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = next(self._ids)
        response = await self._post(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        message = _response_for(rid, response)
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(f"{method} failed: {error.get('message') or error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"{method} returned no result")
        return result


def _brief(response: httpx.Response) -> str:
    return " ".join(response.text.split())[:200] or response.reason_phrase


def _response_for(rid: int, response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC response with id `rid`, from a JSON body or an event stream."""
    kind = response.headers.get("content-type", "")
    if "text/event-stream" in kind:
        for message in _events(response.text):
            if message.get("id") == rid:
                return message
        raise MCPError("the connector's event stream ended without an answer")
    try:
        body = response.json()
    except ValueError as exc:
        raise MCPError(f"the connector did not answer in JSON: {_brief(response)}") from exc
    if isinstance(body, list):
        body = next((m for m in body if isinstance(m, dict) and m.get("id") == rid), {})
    if not isinstance(body, dict):
        raise MCPError("the connector's answer was not a JSON-RPC message")
    return body


def _events(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if not data:
            continue
        try:
            parsed = json.loads(data)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def result_text(result: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    """A `tools/call` result as text, and whether the tool reported an error. Text parts
    are kept; images and audio are named; a structured result is shown as JSON when
    there is no text."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
        elif kind == "resource_link":
            parts.append(f"(link: {item.get('name') or ''} {item.get('uri') or ''})")
        elif kind in ("image", "audio"):
            parts.append(f"({kind} returned — not shown)")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    text = "\n".join(p for p in parts if p).strip()
    if len(text) > limit:
        text = text[:limit] + "\n… (cut)"
    return text, bool(result.get("isError"))


Connect = Callable[[str, Auth], MCPClient]
"""How a gateway tool opens a session — swapped in tests for one over an in-process app."""


def connect(url: str, auth: Auth) -> MCPClient:
    return MCPClient(url, auth)
