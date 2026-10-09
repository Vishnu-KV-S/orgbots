"""Connectors — apps a bot uses directly, through the Model Context Protocol. Pure values.

A browser can reach almost any app, slowly and by reading pages meant for people. A
connector is the app's own interface for agents: an MCP server — GitHub's, Linear's, a
company's own — that lists its tools with typed arguments and answers calls with data.
A person installs one for the whole organization (every bot can use it), from the
marketplace or by URL, with a token if the server wants one; the runtime connects,
lists the tools, and keeps that list so a bot's prompt can name them. A bot calls one
with `use_connector`; the browser stays the fallback for everything without one.

**A call is gated like a click.** A tool the server marks read-only
(`annotations.readOnlyHint`) runs; anything else asks the person first, unless they
allowed that connector, and a "never" rule refuses it. Auto Review checks a call that is
not read-only. The token is opened in the gateway for the call and dropped: it is never
in a prompt, a run's state, a log line or the computer.

**What a connector returns is data.** Tool output is written by the app — an issue's
text, a page from a wiki — and reaches the prompt fenced, like a page.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

AuthKind = Literal["none", "bearer", "header"]

MAX_CONNECTORS = 30
TOOLS_IN_PROMPT = 60
DESCRIPTION_CHARS = 140
RESULT_IN_PROMPT = 4_000

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


class ConnectorError(ValueError):
    """A connector that cannot be added or used as asked. Shown to whoever asked."""


def connector_name(text: str) -> str:
    """`GitHub (work)` → `github-work`: what a bot writes in `connector`."""
    name = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    if not _NAME.match(name):
        raise ConnectorError(f"{text!r} cannot be a connector name; use letters and digits")
    return name


def read_only(tool: dict[str, Any]) -> bool:
    return bool((tool.get("annotations") or {}).get("readOnlyHint"))


def signature(tool: dict[str, Any]) -> str:
    """`create_issue(owner*, repo*, title*, body)` — required arguments starred."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = ", ".join(f"{k}{'*' if k in required else ''}" for k in list(props)[:12])
    if len(props) > 12:
        args += ", …"
    return f"{tool.get('name', '?')}({args})"


def render_connectors(connectors: Sequence[tuple[str, str, list[dict[str, Any]]]]) -> str:
    """For a bot's system prompt: each connector and its tools, compactly. `(name,
    title, tools)` per connector."""
    if not connectors:
        return ""
    lines = [
        "Connected apps — use_connector calls their tools directly (prefer this to the "
        "browser for these apps; * = required argument, [read] = read-only):"
    ]
    shown = 0
    for name, title, tools in connectors:
        lines.append(f"- {name}" + (f" ({title})" if title and title != name else "") + ":")
        for tool in tools:
            if shown >= TOOLS_IN_PROMPT:
                break
            about = " ".join(str(tool.get("description") or "").split())[:DESCRIPTION_CHARS]
            mark = " [read]" if read_only(tool) else ""
            lines.append(f"    {signature(tool)}{mark}" + (f" — {about}" if about else ""))
            shown += 1
    total = sum(len(t) for _, _, t in connectors)
    if total > shown:
        lines.append(f"  (… {total - shown} more tools; call one by name if you know it)")
    return "\n".join(lines)


def find_tool(tools: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    wanted = name.strip().lower()
    return next((t for t in tools if str(t.get("name", "")).lower() == wanted), None)
