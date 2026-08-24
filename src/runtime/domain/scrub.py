"""Secret scrubbing.

T38: a secret that appears in a tool result must be gone before that value reaches
state, a checkpoint, a log line or an artifact. Four destinations, and the reason
they are listed separately is that they leak differently — a log ships to a third
party, a checkpoint is replayed for months, an artifact is handed to a human, and
state is re-serialised into every subsequent prompt.

Two mechanisms, and the first is the one that actually works:

**Exact-value redaction.** `SecretRegistry` holds the credential values this process
has decrypted. The credential broker registers every value it hands out, so scrubbing
is a string search for something we know is a secret rather than a guess about what
one looks like. This catches the case that matters — our own key echoed back by a
provider error message — with no false negatives and no false positives.

**Pattern redaction**, as a backstop, for secrets that are not ours: a bearer token
in a fetched page, a private key block in a scraped file. Patterns are guesses and
they are documented as such. They run second and they are deliberately conservative:
a false positive here corrupts a deliverable, which is worse than a third party's
token surviving in a report.

The registry is process-global rather than per-run and that is intentional. Scrubbing
must work in code paths that have no `RunContext` — a log formatter, an artifact
writer — and a secret is a secret regardless of which run fetched it. Values are held
only as salted digests plus length, never as plaintext, so a heap dump of the registry
is not a second copy of the credentials.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

REDACTED = "[REDACTED]"

MIN_SECRET_LENGTH = 8
"""Below this, exact-match redaction does more harm than good: a four-character
secret would redact its every appearance as an ordinary substring."""


class SecretRegistry:
    """Values known to be secret. Held as digests, never as plaintext."""

    def __init__(self) -> None:
        self._salt = secrets.token_bytes(16)
        self._digests: dict[str, str] = {}
        """digest → label, for the audit line that says *which* credential leaked."""
        self._by_length: dict[int, set[str]] = {}
        """Length-bucketed digests. Scrubbing a 40KB page against N secrets has to
        avoid being O(N x page), so the search is driven by candidate substrings of
        known secret lengths rather than by N passes over the text."""
        self._first_chars: set[str] = set()
        """First characters of every registered secret. Hashing a window is ~1µs and
        a 32KB artifact has 32K windows per length; this rejects almost all of them
        with a set membership test first. Without it, scrubbing shows up in §9's
        latency number for no reason."""

    def register(self, value: str, *, label: str) -> None:
        if len(value) < MIN_SECRET_LENGTH:
            return
        digest = self._digest(value)
        self._digests[digest] = label
        self._by_length.setdefault(len(value), set()).add(digest)
        self._first_chars.add(value[0])

    def forget_all(self) -> None:
        self._digests.clear()
        self._by_length.clear()
        self._first_chars.clear()

    def _digest(self, value: str) -> str:
        return hashlib.blake2b(self._salt + value.encode(), digest_size=16).hexdigest()

    def label_for(self, value: str) -> str | None:
        return self._digests.get(self._digest(value))

    def scrub(self, text: str) -> tuple[str, list[str]]:
        """Redact every registered secret. Returns the text and the labels hit."""
        if not self._by_length or not text:
            return text, []
        hits: list[str] = []
        out = text
        for length in sorted(self._by_length, reverse=True):
            if length > len(out):
                continue
            # Walk candidate windows of exactly this length. Only windows whose
            # digest matches are secrets, so this never touches ordinary text.
            index = 0
            rebuilt: list[str] = []
            while index <= len(out) - length:
                if out[index] not in self._first_chars:
                    rebuilt.append(out[index])
                    index += 1
                    continue
                window = out[index : index + length]
                label = self._digests.get(self._digest(window))
                if label is not None:
                    hits.append(label)
                    rebuilt.append(REDACTED)
                    index += length
                else:
                    rebuilt.append(out[index])
                    index += 1
            rebuilt.append(out[index:])
            out = "".join(rebuilt)
        return out, hits


_REGISTRY = SecretRegistry()


def registry() -> SecretRegistry:
    return _REGISTRY


# --- pattern backstop ---------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer_header", re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]{16,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
]
"""Guesses, and labelled as such. Ordered so the most specific match first — a key
that matches two patterns should be reported under the narrower name, because the
label ends up in an audit row somebody has to act on."""


def scrub_text(text: str) -> tuple[str, list[str]]:
    """Registered secrets first, patterns second. Returns `(text, labels_hit)`."""
    out, hits = _REGISTRY.scrub(text)
    for label, pattern in _PATTERNS:
        out, count = pattern.subn(
            lambda m, label=label: (  # type: ignore[misc]
                f"{m.group(1)}{REDACTED}" if label == "bearer_header" else REDACTED
            ),
            out,
        )
        if count:
            hits.extend([label] * count)
    return out, hits


def scrub(value: Any) -> tuple[Any, list[str]]:
    """Walk a JSON-shaped value, redacting as we go.

    Keys are scrubbed as well as values. A provider that echoes a request back with
    the key as a *field name* is not hypothetical, and a scrubber that only looked at
    values would write it to the artifact store.
    """
    hits: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            out, found = scrub_text(node)
            hits.extend(found)
            return out
        if isinstance(node, dict):
            return {walk(k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, tuple):
            return tuple(walk(v) for v in node)
        return node

    return walk(value), hits
