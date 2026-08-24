"""Errors the config plane raises.

All of them are `SpecError` — the boot path and the CLI already catch that one type,
and a control plane that raised a new family would be a control plane whose failures
escaped every existing handler.

Every error carries **where**: the file, the kind and the name. M4 §13's first risk is
*"a quiet bad apply — no crash, just an actor with different permissions"*, and the
counterpart risk is a loud one nobody can locate. `at()` is what puts the file path in
front of the message rather than in a traceback.
"""

from __future__ import annotations

from runtime.domain.errors import SpecError


class SpecDocumentError(SpecError):
    """A document is malformed: bad envelope, unknown kind, structural failure.

    Raised by the loader, before any cross-document question can be asked.
    """


class SpecValidationError(SpecError):
    """A document set is well-formed but not coherent — the §6 checks.

    Separate from `SpecDocumentError` because the two are found at different times and
    fixed by different people: a structural error is a typo in one file, a semantic one
    is usually two files disagreeing.
    """


class InlineSecretError(SpecValidationError):
    """A literal-looking credential appeared in a document (§3, edge case 82).

    An error rather than a warning, always. A warning about a secret in git is a
    warning about a secret that is already in git.
    """


class UnpinnedReferenceError(SpecValidationError):
    """A tool or graph reference with no `@version` (§3, edge case 83).

    M0's RunSpec pinning is what makes "which version did this run use" answerable.
    YAML must not become the hole in it.
    """


class RenameRefused(SpecValidationError):
    """A rename would delete an actor with run history (§4b, edge case 77).

    Renaming is a delete plus a create: the new actor has no version history, no
    memory and no task history. `--rename old=new` is how somebody says they meant it.
    """


class PlanStale(SpecError):
    """The plan being applied no longer describes live state (edge case 76).

    Either somebody applied something in between — the recomputed `plan_hash` moved —
    or the plan is older than the staleness window. Both mean the diff a human read is
    not the diff that would be applied.
    """


class ApplyConflict(SpecError):
    """Another apply holds the organization's advisory lock (edge case 75)."""


def at(where: str, message: str) -> str:
    """`file: kind/name: message`. Used everywhere so failures read the same."""
    return f"{where}: {message}"


__all__ = [
    "ApplyConflict",
    "InlineSecretError",
    "PlanStale",
    "RenameRefused",
    "SpecDocumentError",
    "SpecValidationError",
    "UnpinnedReferenceError",
    "at",
]
