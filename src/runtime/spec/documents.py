"""The document format.

One kind per document, many documents per file, separated by `---` (§3). The envelope
is Kubernetes-shaped because that shape is well understood and because the parts it
separates are genuinely different: `metadata` is identity, `spec` is intent.

    apiVersion: agent-platform/v1
    kind: Actor
    metadata:
      name: research
    spec:
      kind: llm_agent
      graph: research@1
      tools: [web.search@1, web.fetch@1]

**Field names are camelCase in YAML and snake_case in Python.** One alias generator,
declared once on `Spec`, rather than a hand-written alias per field — a hand-written
alias is a place for a typo that makes a field silently unsettable, and `extra="forbid"`
would then reject the correct spelling.

**`extra="forbid"` everywhere, deliberately.** The same argument `domain.specs.Frozen`
makes: a typo in a field that was supposed to constrain something must fail
compilation rather than be carried along and ignored. In a config plane the stakes are
higher than in code, because the typo is in a file nobody type-checks.

**Three rules are enforced in the model layer rather than in `validation`**, because
they are properties of a single document and finding them early gives a better message:
version pinning on references, the absence of inline secrets, and the shape of an
actor's kind. Everything that needs to see *another* document lives in `validation`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from runtime.domain.enums import ActorKind, AuthorityLevel, CatchupPolicy, MemoryScope, OnExpiry
from runtime.spec.errors import InlineSecretError, SpecDocumentError, UnpinnedReferenceError

API_VERSION = "agent-platform/v1"
"""The only value `apiVersion` may take. A document that names another is refused
rather than best-effort parsed: a format change that this build cannot represent must
not be half-applied."""

NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
"""Identity keys. Lowercase because they end up in URLs, log lines, pool ids and
`uuid5` derivations, and a name that differs only by case would derive a different id
while looking identical in a diff.

Anchored with `^`/`$` rather than `\\A`/`\\Z` because Pydantic compiles a `pattern=`
with the Rust regex engine, which has no `\\A`. Every use on the Python side is
`fullmatch`, so the two engines agree."""

PINNED_PATTERN = r"^[a-z0-9][a-z0-9._-]*@[0-9]+$"
"""`web.search@1`, `research@1`. §3: an unpinned reference is a validation error."""

NAME_RE = re.compile(NAME_PATTERN)
PINNED_RE = re.compile(PINNED_PATTERN)


class Kind(StrEnum):
    """The document kinds. Closed — an unknown kind is an error, not a passthrough."""

    ORGANIZATION = "Organization"
    DEPARTMENT = "Department"
    ROLE = "Role"
    ACTOR = "Actor"
    MODEL_PROFILE = "ModelProfile"
    MEMORY_PROFILE = "MemoryProfile"
    TOOL_GRANT = "ToolGrant"
    CONNECTION = "Connection"
    BUDGET_POLICY = "BudgetPolicy"
    AUTHORITY_POLICY = "AuthorityPolicy"
    TRIGGER = "Trigger"


# --- secret detection ----------------------------------------------------------------

SECRET_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "apikey",
        "api_key",
        "token",
        "secret",
        "password",
        "passwd",
        "privatekey",
        "private_key",
        "accesskey",
        "access_key",
    }
)
"""Keys whose value may never be a bare string. `credentials: {secretRef: name}` only."""

_SECRET_SHAPES = (
    re.compile(r"\Ask-[A-Za-z0-9_-]{16,}\Z"),
    re.compile(r"\A(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}\Z"),
    re.compile(r"\AAKIA[0-9A-Z]{16}\Z"),
    re.compile(r"\Axox[baprs]-[A-Za-z0-9-]{10,}\Z"),
    re.compile(r"\A-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
"""Shapes that are a credential whatever key they hide under. The key check above
catches the honest mistake; this catches the one where somebody renamed the key."""


def scan_for_secrets(value: Any, *, where: str, path: str = "spec") -> None:
    """Refuse a document containing anything that looks like a live credential.

    Two independent checks, because each misses what the other catches. A secret under
    a *named* key is caught even if the value is `"hunter2"`, which no entropy
    heuristic would flag. A secret under an innocent key is caught by its shape.

    Neither is a security boundary — a determined operator can defeat both — and that
    is fine. This is here to stop the accident, and the accident is what puts
    credentials in git.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key)
            child = f"{path}.{text}"
            if text.lower().replace("-", "_") in SECRET_KEYS and isinstance(item, str):
                raise InlineSecretError(
                    f"{where}: {child} is a literal string. Secrets are never inline — "
                    "write `{secretRef: <name>}` and put the value in the credentials "
                    "store (`runtime.cli credentials --put`)."
                )
            scan_for_secrets(item, where=where, path=child)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            scan_for_secrets(item, where=where, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for shape in _SECRET_SHAPES:
            if shape.match(value):
                raise InlineSecretError(
                    f"{where}: {path} looks like a live credential. Secrets are never "
                    "inline — write `{secretRef: <name>}` instead."
                )


def check_pinned(ref: str, *, where: str, what: str) -> str:
    if not PINNED_RE.fullmatch(ref):
        raise UnpinnedReferenceError(
            f"{where}: {what} {ref!r} is not version-pinned. Write {ref}@N — RunSpec "
            "pinning is what makes 'which version did this run use' answerable, and an "
            "unpinned reference in YAML would be the hole in it."
        )
    return ref


# --- base ----------------------------------------------------------------------------


class Spec(BaseModel):
    """Base for every `spec:` body. Immutable, closed, camelCase on the wire."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
        use_enum_values=False,
    )


Name = Annotated[str, Field(pattern=NAME_PATTERN)]
Cents = Annotated[int, Field(ge=0)]


class Metadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: Name
    uid: UUID | None = None
    """§4 option (a), present and unused. See migration 033."""
    labels: dict[str, str] = Field(default_factory=dict)


class SecretRef(BaseModel):
    """The only way a document may name a credential (§3)."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    secret_ref: Name


# --- per-kind specs -------------------------------------------------------------------


class OrganizationDoc(Spec):
    display_name: str | None = None
    description: str = ""


class DepartmentDoc(Spec):
    head: Name | None = None
    """The actor that leads it. Circular by nature — the head names this department
    back — which is why apply is two-phase (§7)."""
    parent: Name | None = None
    description: str = ""


class RoleDoc(Spec):
    rank: int = Field(default=100, ge=0)
    """Advisory seniority. `parent` is authoritative; the acyclicity check asserts the
    two agree rather than trusting either alone (migration 016)."""
    parent: Name | None = None
    department: Name | None = None
    approver: str | None = None
    approver_daily_budget: int = Field(default=10, ge=0)
    base_authority: dict[str, Any] = Field(default_factory=dict)


class ModelProfileDoc(Spec):
    provider: str
    model: str
    max_output_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    input_cents_per_mtok: Cents = 0
    output_cents_per_mtok: Cents = 0
    thinking: bool = True
    effort: Literal["low", "medium", "high", "max"] | None = None
    web_search: bool = False
    """Provider-side search. Needs the actor to hold `web.search@1` as well; the
    validation pass checks that pair rather than letting the request look like a
    permission."""


class MemoryProfileDoc(Spec):
    scopes: tuple[str, ...] = ()
    """`session`, `private`, `company`, `department` or `dept:<name>`. Resolved and
    checked against the actor's own department by `validation` — a scope an actor may
    not hold is edge case 81, scope escalation by config edit."""


class CeilingsDoc(Spec):
    max_llm_calls: int = Field(default=32, ge=0)
    max_tool_calls: int = Field(default=64, ge=0)
    max_wall_clock_s: float = Field(default=300.0, gt=0)
    max_cost_cents: Cents = 500
    max_depth: int = Field(default=4, ge=0)


class DelegationDoc(Spec):
    enabled: bool = False
    max_depth: int = Field(default=0, ge=0)
    max_children: int = Field(default=0, ge=0)
    max_live_descendants: int = Field(default=0, ge=0)
    """Declared in M4, enforced in M5. Recorded here so the org that M5 turns
    delegation on for is one whose limits were already written down and reviewed."""


class ActorAuthorityDoc(Spec):
    """Inline authority on an actor — sugar for actor-scoped `AuthorityPolicy`.

    Two spellings, matching what `org.authority._from_base` already accepts:

        allowed: [research]                    → level auto
        approvalRequired: {publish_external: human}

    A standalone `AuthorityPolicy` document naming the same (actor, action) is a
    conflict rather than an override. Two sources for one row is how a policy comes to
    depend on file ordering, which is the one thing a reviewable diff must not do.
    """

    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    approval_required: dict[str, Literal["human"]] = Field(default_factory=dict)
    approver_role: Name | None = None
    max_escalations: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    on_expiry: OnExpiry = OnExpiry.DENY


class ActorDoc(Spec):
    kind: ActorKind
    department: Name | None = None
    role: Name | None = None
    reports_to: Name | None = None
    graph: str | None = None
    """Pinned registry key, e.g. `research@1`. Required for `llm_agent`."""
    handler: str | None = None
    """Pinned registry key, e.g. `analytics@1`. Required for `deterministic_worker`."""
    tools: tuple[str, ...] = ()
    model_profiles: dict[str, Name] = Field(default_factory=dict)
    """work class → `ModelProfile` document name. An actor with no profile for a class
    *cannot make a call in that class* — this is an allow-list, not a default set, and
    the empty mapping is the meaningful value that makes `analytics` deterministic."""
    memory_profile: Name | None = None
    memory: MemoryProfileDoc | None = None
    """Inline alternative to `memoryProfile`. Naming both is an error."""
    ceilings: CeilingsDoc = CeilingsDoc()
    authority: ActorAuthorityDoc | None = None
    delegation: DelegationDoc | None = None
    allowed_model_call_sites: tuple[str, ...] | None = None
    """HYBRID only, and mandatory there — which is moot, because HYBRID is refused at
    compile (§6). Present so the document format is closed over the eventual design."""

    @field_validator("graph", "handler")
    @classmethod
    def _pinned(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return check_pinned(value, where="Actor", what="reference")

    @field_validator("tools")
    @classmethod
    def _pinned_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tool in value:
            check_pinned(tool, where="Actor", what="tool")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        """The same three rules `ActorSpec._check_shape` enforces, at document level.

        Duplicated on purpose. `ActorSpec` is the last line and stays as it is; saying
        it here as well means the operator gets an error naming the *document* rather
        than a pydantic failure on an object they never wrote.
        """
        if self.kind is ActorKind.LLM_AGENT and not self.graph:
            raise ValueError("kind llm_agent requires `graph`")
        if self.kind is ActorKind.DETERMINISTIC_WORKER:
            if not self.handler:
                raise ValueError("kind deterministic_worker requires `handler`")
            if self.ceilings.max_llm_calls != 0:
                raise ValueError(
                    "kind deterministic_worker requires ceilings.maxLlmCalls == 0 — the "
                    "gateway refusal is the guarantee, not a convention"
                )
            if self.model_profiles:
                raise ValueError(
                    "kind deterministic_worker may not declare modelProfiles; it cannot "
                    "make a model call and a profile would describe one that is refused"
                )
        if self.memory_profile and self.memory:
            raise ValueError("name `memoryProfile` or write `memory` inline, not both")
        if self.kind is not ActorKind.HYBRID and self.allowed_model_call_sites is not None:
            raise ValueError("allowedModelCallSites is meaningful only for kind hybrid")
        return self


class GrantSubject(Spec):
    actor: Name | None = None
    role: Name | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if bool(self.actor) == bool(self.role):
            raise ValueError("a grant subject is exactly one of `actor` or `role`")
        return self

    @property
    def subject_type(self) -> str:
        return "actor" if self.actor else "role"

    @property
    def subject_id(self) -> str:
        return self.actor or self.role or ""


class ToolGrantDoc(Spec):
    subject: GrantSubject
    tool: str
    connection: Name | None = None

    @field_validator("tool")
    @classmethod
    def _pinned(cls, value: str) -> str:
        return check_pinned(value, where="ToolGrant", what="tool")


class ConnectionDoc(Spec):
    provider: str
    scopes: tuple[str, ...] = ()
    credentials: SecretRef | None = None
    status: Literal["ACTIVE", "DISABLED"] = "ACTIVE"


class BudgetPolicyDoc(Spec):
    """Pool limits for the org, its departments and its actors.

    Monthly, matching `budget_pools.period`. Not per-actor-inline, even though §3's
    illustrative example shows `budget: {dailyCents: 3000}` on an actor: the check that
    matters is *children <= parent * (1 + oversubscription)*, and a limit scattered
    across a dozen actor documents makes that sum something a reviewer has to assemble
    by hand from a diff. One document is one place to look.
    """

    period: Literal["month"] = "month"
    oversubscription: float = Field(default=0.0, ge=0.0, le=1.0)
    """v3 §8's soft ceiling `K`. Children may sum to `parent * (1 + K)` because
    allocations are advisory; the hard reservation path is still bounded by `limit` at
    every level, so admitting 1.3x of intent cannot become 1.3x of spend."""
    organization: Cents
    departments: dict[str, Cents] = Field(default_factory=dict)
    actors: dict[str, Cents] = Field(default_factory=dict)


class AuthorityPolicyDoc(Spec):
    scope: Literal["role", "department", "actor"]
    target: Name
    action: str
    level: AuthorityLevel
    approver_role: Name | None = None
    max_escalations: int = Field(default=0, ge=0)
    on_expiry: OnExpiry = OnExpiry.DENY
    ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)


class TriggerDoc(Spec):
    actor: Name
    cron: str
    timezone: str = "UTC"
    mode: str
    """The entry point the actor's graph dispatches on. Lands in the trigger's input
    payload alongside the trigger key, exactly as `department.TriggerSpec.input` builds
    it — so a YAML-defined trigger and a code-defined one produce the same row."""
    catchup: CatchupPolicy = CatchupPolicy.SKIP
    input: dict[str, Any] = Field(default_factory=dict)
    """Extra payload merged under `mode` and `trigger`. Those two always win: a
    trigger whose payload disagreed with its own key would be undiagnosable."""


SPEC_BY_KIND: dict[Kind, type[Spec]] = {
    Kind.ORGANIZATION: OrganizationDoc,
    Kind.DEPARTMENT: DepartmentDoc,
    Kind.ROLE: RoleDoc,
    Kind.ACTOR: ActorDoc,
    Kind.MODEL_PROFILE: ModelProfileDoc,
    Kind.MEMORY_PROFILE: MemoryProfileDoc,
    Kind.TOOL_GRANT: ToolGrantDoc,
    Kind.CONNECTION: ConnectionDoc,
    Kind.BUDGET_POLICY: BudgetPolicyDoc,
    Kind.AUTHORITY_POLICY: AuthorityPolicyDoc,
    Kind.TRIGGER: TriggerDoc,
}


# --- the envelope ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Document:
    """One parsed document, plus where it came from."""

    kind: Kind
    metadata: Metadata
    spec: Spec
    source: str = "<memory>"
    """File path, for error messages. Not hashed — the same document applied from two
    checkouts must produce the same `spec_hash`."""
    source_yaml: str = ""
    """The document's own text, retained for `spec_documents.source_yaml` (§9)."""
    index: int = 0
    """Position within its file, so a `---`-separated document can be pointed at."""

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def where(self) -> str:
        return f"{self.source}[{self.index}] {self.kind.value}/{self.name}"

    @property
    def key(self) -> tuple[Kind, str]:
        return (self.kind, self.metadata.name)

    def body(self) -> dict[str, Any]:
        """The `spec:` body as plain JSON-able data, camelCase, defaults materialised.

        `by_alias=True` and `exclude_none=False` together are what make the exporter
        and the loader round-trip: what comes out is spelled the way what goes in must
        be spelled, and a default that is materialised here is materialised there.
        """
        return self.spec.model_dump(mode="json", by_alias=True)

    def envelope(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": self.metadata.name}
        if self.metadata.uid is not None:
            meta["uid"] = str(self.metadata.uid)
        if self.metadata.labels:
            meta["labels"] = dict(sorted(self.metadata.labels.items()))
        return {
            "apiVersion": API_VERSION,
            "kind": self.kind.value,
            "metadata": meta,
            "spec": self.body(),
        }


@dataclass(frozen=True, slots=True)
class DocumentSet:
    """Every document that was loaded, indexed by `(kind, name)`.

    Ordered by kind and then by name rather than by file, because a diff whose order
    depends on which file somebody happened to edit is not reviewable (edge case 85).
    """

    documents: tuple[Document, ...] = ()
    by_key: dict[tuple[Kind, str], Document] = field(default_factory=dict)

    @classmethod
    def build(cls, documents: list[Document]) -> DocumentSet:
        by_key: dict[tuple[Kind, str], Document] = {}
        for doc in documents:
            existing = by_key.get(doc.key)
            if existing is not None:
                raise SpecDocumentError(
                    f"{doc.where}: duplicate document — {existing.where} already "
                    f"defines {doc.kind.value}/{doc.name}. Identity is "
                    "(kind, metadata.name) within an organization (§4)."
                )
            by_key[doc.key] = doc
        ordered = tuple(
            sorted(documents, key=lambda d: (list(Kind).index(d.kind), d.metadata.name))
        )
        return cls(documents=ordered, by_key=by_key)

    def of(self, kind: Kind) -> tuple[Document, ...]:
        return tuple(d for d in self.documents if d.kind is kind)

    def get(self, kind: Kind, name: str) -> Document | None:
        return self.by_key.get((kind, name))

    def names(self, kind: Kind) -> frozenset[str]:
        return frozenset(d.metadata.name for d in self.of(kind))

    def __iter__(self) -> Iterator[Document]:
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)


# --- memory scope references ----------------------------------------------------------


def parse_scope(token: str, *, where: str) -> tuple[MemoryScope, str | None]:
    """`private` → PRIVATE_ACTOR; `dept:marketing` → (DEPARTMENT, "marketing").

    The qualifier exists so that a scope reference is checkable: `department` alone
    would be checkable only against whichever department the actor happens to be in
    today, and the check that matters (edge case 81) is that the actor may hold the
    scope it *names*.
    """
    raw = token.strip()
    if ":" in raw:
        head, _, qualifier = raw.partition(":")
        if head not in {"dept", "department"}:
            raise SpecDocumentError(
                f"{where}: memory scope {token!r} — only `dept:<name>` takes a qualifier"
            )
        return MemoryScope.DEPARTMENT, qualifier
    aliases = {
        "session": MemoryScope.SESSION,
        "private": MemoryScope.PRIVATE_ACTOR,
        "private_actor": MemoryScope.PRIVATE_ACTOR,
        "department": MemoryScope.DEPARTMENT,
        "company": MemoryScope.COMPANY,
    }
    try:
        return aliases[raw], None
    except KeyError as exc:
        raise SpecDocumentError(
            f"{where}: {token!r} is not a memory scope; expected one of "
            f"{sorted(aliases)} or `dept:<name>`"
        ) from exc


__all__ = [
    "API_VERSION",
    "NAME_PATTERN",
    "NAME_RE",
    "PINNED_PATTERN",
    "PINNED_RE",
    "SECRET_KEYS",
    "SPEC_BY_KIND",
    "ActorAuthorityDoc",
    "ActorDoc",
    "AuthorityPolicyDoc",
    "BudgetPolicyDoc",
    "CeilingsDoc",
    "ConnectionDoc",
    "DelegationDoc",
    "DepartmentDoc",
    "Document",
    "DocumentSet",
    "GrantSubject",
    "Kind",
    "MemoryProfileDoc",
    "Metadata",
    "ModelProfileDoc",
    "OrganizationDoc",
    "RoleDoc",
    "SecretRef",
    "Spec",
    "ToolGrantDoc",
    "TriggerDoc",
    "check_pinned",
    "parse_scope",
    "scan_for_secrets",
]
