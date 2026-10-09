"""Organization policies and team secrets — what an organization's admins decide for all.

**Network.** `open` (the default) lets bots' browsers and commands reach anything, as
the runtime always has. `allowlist` lets them reach only the listed hosts (a host
covers its subdomains: `example.com` allows `docs.example.com`). It is enforced four
times, by whoever is closest to the request: the computer's browser refuses any request
— a page, a redirect, an image, a script — to a host not on the list; the browser tool
refuses a navigation before it is sent, so the bot hears why; sandboxed commands run
without a network at all (a list of hosts cannot be enforced on arbitrary programs
without a proxy, so the honest choice is none); and an app (MCP server) on a host not
on the list cannot be connected or called.

**Auto Review required.** Every bot's risky steps are checked by the reviewer
(`domain.review`), whatever its own switch says.

**Template links** off: no member can publish a bot's setup by link, and links already
made stop working until it is on again. **Members add apps** off (the default with
members): only owners and admins connect apps, whose tokens are the organization's.

**Team secrets** are values sandboxed commands read as environment variables — a CLI's
API token, say. The value is sealed, never shown again, registered with the scrubber
so a command that prints it returns `[REDACTED]`, and never in a prompt: a bot is told
only the names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

Network = Literal["open", "allowlist"]
MAX_HOSTS = 200

SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_SECRETS = 100
MAX_SECRET_BYTES = 32 * 1024
MAX_SECRETS_BYTES = 96 * 1024
RESERVED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "WORKSPACE",
        "PWD",
        "LANG",
        "TERM",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "BASH_ENV",
        "ENV",
    }
)
"""Names the sandbox sets itself, or that change how every program runs."""

_HOST = re.compile(r"^(?=.{1,253}$)(\*\.)?([a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}$")
_NO_NETWORK_SCHEMES = frozenset({"about", "data", "blob", "javascript", "chrome-error"})


class PolicyError(ValueError):
    """A policy or a secret that cannot be saved. Shown to the admin."""


@dataclass(frozen=True)
class Policy:
    network: Network = "open"
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    require_review: bool = False
    template_links: bool = True
    members_add_apps: bool = False

    def host_allowed(self, host: str) -> bool:
        if self.network == "open":
            return True
        host = (host or "").lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.allowed_hosts)

    def url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in _NO_NETWORK_SCHEMES or not parsed.scheme:
            return True
        if self.network == "open":
            return True
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            return False
        return self.host_allowed(parsed.hostname or "")


DEFAULT = Policy()


def clean_hosts(values: list[str]) -> list[str]:
    """`https://Docs.Example.com/path` → `docs.example.com`; `*.x.com` → `x.com`."""
    out: list[str] = []
    for raw in values:
        text = raw.strip().lower()
        if not text:
            continue
        if "://" in text:
            text = urlparse(text).hostname or ""
        text = text.split("/", 1)[0].split(":", 1)[0].rstrip(".")
        if not _HOST.match(text) or "." not in text.removeprefix("*."):
            raise PolicyError(f"{raw.strip()!r} is not a host name")
        text = text.removeprefix("*.")
        if text not in out:
            out.append(text)
    if len(out) > MAX_HOSTS:
        raise PolicyError(f"an allowlist holds at most {MAX_HOSTS} hosts")
    return out


def check_secret(name: str, value: str, *, others: dict[str, int]) -> None:
    """`others` is the byte size of every other secret, by name."""
    if not SECRET_NAME.match(name):
        raise PolicyError(
            f"{name!r} is not an environment variable name: capitals, digits and _, "
            "not starting with a digit"
        )
    if name in RESERVED_NAMES or name.startswith("RUNTIME_"):
        raise PolicyError(f"{name} is set by the sandbox itself; pick another name")
    size = len(value.encode())
    if not value:
        raise PolicyError("a secret needs a value")
    if size > MAX_SECRET_BYTES:
        raise PolicyError(f"a secret is at most {MAX_SECRET_BYTES // 1024} KB")
    if name not in others and len(others) >= MAX_SECRETS:
        raise PolicyError(f"an organization has at most {MAX_SECRETS} secrets")
    if sum(others.values()) + size > MAX_SECRETS_BYTES:
        raise PolicyError(f"all secrets together are at most {MAX_SECRETS_BYTES // 1024} KB")
