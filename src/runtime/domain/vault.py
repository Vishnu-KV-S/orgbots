"""The login vault — the values. Pure: no I/O, no keys, no plaintext.

A bot that meets a sign-in, sign-up or verification-code form does not type into it.
It says `sign_in`, and the runtime takes over:

1. **Read the form off the page.** `form_fields` turns the page snapshot into the
   fields a person would be asked for — email, password, the six boxes of a code —
   from the inputs' own types, `autocomplete` tokens and labels. The card a person
   sees is built from the page, not from the model's account of the page, the same
   rule the approval card keeps.
2. **Fill it from the vault, or ask.** A saved login for this exact site fills it; a
   field nothing saved can answer (a code, a sign-up's name) is asked for in the chat
   with a form whose values go to the vault and from there into the browser. The
   model is told *that* the form was filled, never *with what*.

Everything a run, a checkpoint, a journal row or a prompt holds is from this module's
vocabulary: entry ids, field kinds, element numbers, a host. The secrets themselves
live encrypted in `vault_entries` and are opened by `runtime.gateway.vault` in the one
place that types them, which is also where the host check that matters is made.

**Same site means the same host.** A login captured on `accounts.example.com` is
offered on `accounts.example.com` (and its `www.` twin) and nowhere else — not on a
sibling subdomain, not on a lookalike. Registrable-domain matching would be friendlier
and it is wrong on shared hosting (`alice.github.io` and `mallory.github.io` share
one), so this is the strict rule a careful password manager uses, and a site that
signs in across two hosts asks twice.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

FieldKind = Literal[
    "email",
    "username",
    "phone",
    "password",
    "new_password",
    "confirm_password",
    "otp",
    "name",
    "text",
]

Purpose = Literal["sign_in", "sign_up", "verify"]

IDENTITY_KINDS: frozenset[str] = frozenset({"email", "username", "phone"})
SAVED_KINDS: frozenset[str] = IDENTITY_KINDS | {"password"}
"""What a saved login keeps: who you are and the password. Never a one-time code,
which is worthless a minute later, and never a sign-up's name or company, which are
not credentials."""

SECRET_KINDS: frozenset[str] = frozenset({"password", "new_password", "confirm_password", "otp"})
"""Kinds whose input must be a password box (`otp` excepted) and whose value is never
shown, not even as a hint."""

PASSWORD_KINDS: frozenset[str] = frozenset({"password", "new_password", "confirm_password"})

ONCE_TTL_S = 600
"""How long a value a person typed but did not save stays usable. Long enough for a
two-page sign-in (email, then password) or a code that arrives a minute late; short
enough that an unsaved password is not a saved one by another name."""

MAX_VALUE_CHARS = 512

FILLABLE_TYPES = frozenset({"", "text", "email", "tel", "password", "number"})

_OTP = re.compile(
    r"one.?time|\botp\b|verif\w*\s*code|security\s*code|confirmation\s*code|2fa|"
    r"two.?(factor|step)|auth\w*\s*code|\bcode\b|passcode|\btoken\b",
    re.I,
)
_CONFIRM = re.compile(r"confirm|again|repeat|re.?type|re.?enter|verify", re.I)
_NEW = re.compile(r"\bnew\b|create|choose|set\s+a|sign.?up|register", re.I)
_EMAIL = re.compile(r"e.?mail", re.I)
_USERNAME = re.compile(r"user.?name|\buser\b|login|log.?in\s*id|account|handle|\bid\b", re.I)
_PHONE = re.compile(r"phone|mobile|\bcell\b", re.I)
_NAME = re.compile(r"\bname\b|first|last|surname|given|family", re.I)
_SEARCH = re.compile(r"search|\bfind\b|\bquery\b", re.I)
_PAYMENT = re.compile(r"card|cvv|cvc|expir|billing|iban", re.I)


@dataclass(frozen=True, slots=True)
class CredentialField:
    """One thing to ask for. `elements` are snapshot numbers — several for a code
    split into one box per digit, none for a password asked for ahead of the page
    that wants it (a two-step sign-in)."""

    key: str
    kind: FieldKind
    label: str
    elements: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "elements": list(self.elements),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> CredentialField:
        kind = str(raw.get("kind", "text"))
        elements = raw.get("elements")
        return cls(
            key=str(raw["key"]),
            kind=kind if kind in _KINDS else "text",  # type: ignore[arg-type]
            label=str(raw.get("label", "")),
            elements=tuple(_int(e) for e in elements) if isinstance(elements, list) else (),
        )


_KINDS = frozenset(FieldKind.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class VaultOption:
    """A vault entry as the graph sees it: what it can answer, never the answers."""

    id: uuid.UUID
    kind: Literal["saved", "once"]
    host: str
    kinds: frozenset[str]
    label: str = ""
    auto_use: bool = True


@dataclass(frozen=True, slots=True)
class FillPlan:
    """What a fill is made of: the entries to open and where each value goes."""

    entries: tuple[uuid.UUID, ...]
    fields: tuple[CredentialField, ...]
    via: str
    """`saved` or `once` — said in the conversation."""
    label: str = ""

    def signature(self, host: str) -> str:
        """Same site, same fields: the second time is a failed login, not the second
        page of one (email → password changes the fields). Entries are left out on
        purpose — a person who saves the login they just typed has two entries with
        one password in them, and trying the second is the same wrong password twice."""
        kinds = ",".join(sorted(f.kind for f in self.fields if f.elements))
        return f"{site_of(host)}|{kinds}"


# --- hosts ------------------------------------------------------------------------------


def site_of(host: str) -> str:
    """The host a vault entry is filed under: lowercased, without `www.`."""
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(entry_host: str, page_host: str) -> bool:
    return bool(entry_host) and site_of(entry_host) == site_of(page_host)


# --- reading a form ---------------------------------------------------------------------


def _text(element: dict[str, object]) -> str:
    return " ".join(
        str(element.get(k) or "") for k in ("label", "name", "autocomplete", "role")
    ).strip()


def classify(element: dict[str, object]) -> FieldKind | None:
    """What a person would be asked to put in this input, or `None` if it is not part
    of a credential form (a checkbox, a search box, a card number).

    `autocomplete` first, because it is the site telling password managers exactly
    this; then the input type; then the label. Payment fields are refused outright:
    they belong to the approval card and a person's own hands, not a login vault.
    """
    if str(element.get("tag", "")) != "input":
        return None
    kind = str(element.get("type") or "").lower()
    if kind not in FILLABLE_TYPES:
        return None
    auto = str(element.get("autocomplete") or "").lower()
    words = _text(element)
    if auto.startswith("cc-") or _PAYMENT.search(words):
        return None
    if "one-time-code" in auto:
        return "otp"
    if "new-password" in auto:
        return "confirm_password" if _CONFIRM.search(words) else "new_password"
    if "current-password" in auto:
        return "password"
    if kind == "password":
        if _OTP.search(words):
            return "otp"
        if _CONFIRM.search(words):
            return "confirm_password"
        if _NEW.search(words):
            return "new_password"
        return "password"
    if "username" in auto.split():
        return "email" if kind == "email" else "username"
    if auto.endswith("email") or kind == "email":
        return "email"
    if auto.startswith("tel") or kind == "tel":
        return "otp" if _OTP.search(words) else "phone"
    if _SEARCH.search(words):
        return None
    if _OTP.search(words) or _is_code_box(element):
        return "otp"
    if _EMAIL.search(words):
        return "email"
    if _PHONE.search(words):
        return "phone"
    if _USERNAME.search(words):
        return "username"
    if "name" in auto or _NAME.search(str(element.get("label") or "")):
        return "name"
    return "text"


def _is_code_box(element: dict[str, object]) -> bool:
    """One box of a code split across several — `maxlength=1`, usually numeric."""
    return _int(element.get("maxlength")) == 1


def _int(value: object) -> int:
    """A number from a snapshot, where the page decides what is in each slot."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str | float):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


_LABELS: dict[str, str] = {
    "email": "Email",
    "username": "Username",
    "phone": "Phone number",
    "password": "Password",
    "new_password": "New password",
    "confirm_password": "Confirm password",
    "otp": "Verification code",
    "name": "Name",
    "text": "Value",
}


def form_fields(
    elements: list[dict[str, object]], anchor: int | None = None
) -> list[CredentialField]:
    """The credential form on this page, as fields to ask for.

    The form is the one holding `anchor` (an element the bot pointed at) or, when the
    bot named none, the first form with a password, code or email box. Inputs outside
    a `<form>` are treated as one form — single-page apps rarely use the tag — and
    neighbouring one-character boxes are merged into one code.
    """
    fillable = [(e, classify(e)) for e in elements]
    candidates = [(e, k) for e, k in fillable if k is not None]
    if not candidates:
        return []
    by_id = {_int(e.get("id")): e for e, _ in candidates}
    if anchor is not None and anchor in by_id:
        form = by_id[anchor].get("form", -1)
    else:
        strong = next(
            (e for e, k in candidates if k in SECRET_KINDS or k == "email"), candidates[0][0]
        )
        form = strong.get("form", -1)
    members = [(e, k) for e, k in candidates if e.get("form", -1) == form]

    fields: list[CredentialField] = []
    pending_code: list[int] = []

    def flush_code() -> None:
        if pending_code:
            fields.append(
                CredentialField(
                    key=f"f{len(fields)}",
                    kind="otp",
                    label=_LABELS["otp"],
                    elements=tuple(pending_code),
                )
            )
            pending_code.clear()

    for element, kind in members:
        number = _int(element.get("id"))
        if kind == "otp" and _is_code_box(element):
            pending_code.append(number)
            continue
        flush_code()
        label = " ".join(str(element.get("label") or "").split())[:80] or _LABELS[kind]
        fields.append(
            CredentialField(key=f"f{len(fields)}", kind=kind, label=label, elements=(number,))
        )
    flush_code()
    return fields


def purpose_of(fields: list[CredentialField]) -> Purpose:
    kinds = {f.kind for f in fields}
    if kinds & {"new_password", "confirm_password"}:
        return "sign_up"
    if kinds <= {"otp"}:
        return "verify"
    return "sign_in"


def with_next_page(fields: list[CredentialField]) -> list[CredentialField]:
    """A sign-in page that asks only who you are gets a password field added.

    Two-step sign-ins (email → Next → password) would otherwise ask the person twice,
    a page apart. The extra field has no elements: it is filled on the next page, from
    the same entry, when that page asks for it.
    """
    kinds = {f.kind for f in fields}
    if kinds and kinds <= IDENTITY_KINDS:
        return [*fields, CredentialField(key=f"f{len(fields)}", kind="password", label="Password")]
    return fields


def asked(fields: list[CredentialField]) -> list[CredentialField]:
    """The fields a person types. A confirm box is filled with the new password —
    asking twice in a form the person cannot mistype into a page is noise."""
    has_new = any(f.kind == "new_password" for f in fields)
    return [f for f in fields if not (f.kind == "confirm_password" and has_new)]


# --- answering from the vault -----------------------------------------------------------


def lookup(kind: str, values: dict[str, str], key: str = "") -> str | None:
    """The value for one field from an entry's values.

    A field's own key first (an unsaved sign-up's "Company"), then its kind, then the
    substitutions a person would make without thinking: a "username" box takes an
    email address, a new-password box takes the password, a confirm box takes whichever
    password the entry has.
    """
    if key and values.get(f"field:{key}"):
        return values[f"field:{key}"]
    order: dict[str, tuple[str, ...]] = {
        "username": ("username", "email", "phone"),
        "email": ("email", "username"),
        "phone": ("phone",),
        "password": ("password", "new_password"),
        "new_password": ("new_password", "password"),
        "confirm_password": ("confirm_password", "new_password", "password"),
        "otp": ("otp",),
        "name": ("name",),
    }
    for candidate in order.get(kind, ()):
        if values.get(candidate):
            return values[candidate]
    return None


def answerable(kind: str, kinds: frozenset[str]) -> bool:
    """`lookup` on metadata: could an entry holding `kinds` fill this field?"""
    probe = dict.fromkeys(kinds, "x")
    return lookup(kind, probe) is not None


def plan_fill(
    fields: list[CredentialField], options: list[VaultOption], *, tried: set[str], host: str
) -> FillPlan | None:
    """Fill from the vault, if the vault can answer every field on the page.

    Fresh values the person just gave this bot come first — they are the reason the
    form is showing — then saved logins the person allowed to be used automatically.
    A plan already tried this turn is not tried again: the same form coming back
    after it is a wrong password, and filling it a second time is how an account gets
    locked.
    """
    wanted = [f for f in fields if f.elements]
    if not wanted:
        return None
    ordered = [o for o in options if o.kind == "once"] + [
        o for o in options if o.kind == "saved" and o.auto_use
    ]
    for option in ordered:
        if not same_site(option.host, host):
            continue
        if all(answerable(f.kind, option.kinds) for f in wanted):
            plan = FillPlan(
                entries=(option.id,),
                fields=tuple(wanted),
                via=option.kind,
                label=option.label,
            )
            if plan.signature(host) not in tried:
                return plan
    return None


def split_values(
    fields: list[CredentialField], submitted: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """A person's form → `(once, saved)` value maps.

    `once` holds everything, for this fill and the next page of it. `saved` holds the
    identity and the password, for next time — a sign-up's new password is saved as
    the password, because the next time this site is seen it is a sign-in.
    """
    once: dict[str, str] = {}
    for f in fields:
        value = submitted.get(f.key, "")
        if not value:
            continue
        if f.kind in ("text", "name"):
            once[f"field:{f.key}"] = value
        if f.kind != "text":
            once.setdefault(f.kind, value)
    if "new_password" in once:
        once.setdefault("password", once["new_password"])
    saved = {k: v for k, v in once.items() if k in SAVED_KINDS}
    if "password" not in saved:
        saved = {}
    return once, saved


def hint(values: dict[str, str]) -> str:
    """How a saved login is named in a list: enough to tell two apart, not to use one.

    `ada@example.com` → `a••@example.com`; a phone shows its last two digits.
    """
    for kind in ("email", "username", "phone"):
        value = values.get(kind, "").strip()
        if not value:
            continue
        if kind == "email" and "@" in value:
            local, _, domain = value.partition("@")
            return f"{local[:1]}••@{domain}"
        if kind == "phone":
            digits = re.sub(r"\D", "", value)
            return f"•••{digits[-2:]}" if digits else "•••"
        return f"{value[:2]}••" if len(value) > 3 else "•••"
    return "saved login"


def check_value(kind: str, value: str) -> str | None:
    """Why a submitted value is refused, or `None`. Mild on purpose: the site is the
    authority on what it accepts, and this only stops what cannot be meant."""
    if len(value) > MAX_VALUE_CHARS:
        return f"is longer than {MAX_VALUE_CHARS} characters"
    if any(ch in value for ch in "\r\n\x00"):
        return "contains a line break"
    if kind == "otp" and not re.fullmatch(r"[A-Za-z0-9\- ]{3,12}", value):
        return "does not look like a verification code"
    return None


# --- identities -------------------------------------------------------------------------


def request_id(run_id: object, step: int) -> uuid.UUID:
    """A credential request a run's step made. Derived, so a replayed step re-writes
    the same request instead of asking twice."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botcreds:{run_id}:{step}")


def vault_aad(organization_id: object, entry_id: object, host: str) -> bytes:
    """Binds a ciphertext to its organization, its row and its site. A row copied to
    another organization, or re-pointed at another host, fails to open rather than
    typing one site's password into another's form."""
    return f"vault:{organization_id}:{entry_id}:{site_of(host)}".encode()
