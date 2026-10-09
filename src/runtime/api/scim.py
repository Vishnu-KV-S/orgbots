"""SCIM 2.0 (RFC 7643/7644) — `/scim/v2`, for an identity provider, not a browser.

Like `/v1/hooks`, this is called from outside and is not behind the UI's proxy: give the
provider `{RUNTIME_PUBLIC_URL}/scim/v2` and the token from **Team → Provisioning**. It
is authenticated by that bearer token alone, compared as a hash, and every answer is
scoped to the token's organization.

Users only (no Groups), as members: `userName` is the email, `name`/`displayName` the
name, `active` whether they may sign in. Filtering supports `userName eq "…"` (and
`externalId`, `emails.value`), which is what Okta and Microsoft Entra send before
creating someone. `PATCH` takes both providers' shapes — a `path` with a value
(`"active"`, `"False"` as a string included) or a value object with no path. `DELETE`
deactivates (`runtime.runtime.scim` says why nobody is deleted).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from runtime.domain.members import MemberError
from runtime.persistence.repositories.members import MemberRow
from runtime.runtime.scim import Changes, ScimConflictError, ScimNotFoundError, ScimService
from runtime.settings import Settings

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
MEDIA = "application/scim+json"
MAX_RESULTS = 200

_FILTER = re.compile(r'^\s*(userName|externalId|emails\.value)\s+eq\s+"([^"]*)"\s*$', re.I)


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str = "") -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def scim_error(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [ERROR], "status": str(exc.status), "detail": exc.detail}
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(body, status_code=exc.status, media_type=MEDIA)


def _service(request: Request) -> ScimService:
    service = getattr(request.app.state, "scim", None)
    if service is None:
        service = ScimService(request.app.state.uow)
        request.app.state.scim = service
    return service


async def _org(request: Request) -> uuid.UUID:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    org = await _service(request).organization_for(token)
    if org is None:
        raise ScimError(401, "a valid bearer token is required")
    return org


def _base(request: Request) -> str:
    settings: Settings = request.app.state.settings
    return (settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")) + "/scim/v2"


def _user(request: Request, row: MemberRow) -> dict[str, Any]:
    given, _, family = row.name.partition(" ")
    out: dict[str, Any] = {
        "schemas": [USER],
        "id": str(row.id),
        "userName": row.email,
        "name": {"formatted": row.name, "givenName": given, "familyName": family},
        "displayName": row.name or row.email,
        "emails": [{"value": row.email, "primary": True, "type": "work"}],
        "active": row.status == "active",
        "meta": {
            "resourceType": "User",
            "created": row.created_at.isoformat(),
            "lastModified": (row.last_seen_at or row.created_at).isoformat(),
            "location": f"{_base(request)}/Users/{row.id}",
        },
    }
    if row.external_id:
        out["externalId"] = row.external_id
    return out


def _answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ScimError(400, "the body is not JSON", "invalidSyntax") from exc
    if not isinstance(body, dict):
        raise ScimError(400, "the body is not a JSON object", "invalidSyntax")
    return body


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _email_of(body: dict[str, Any]) -> str | None:
    emails = body.get("emails")
    if isinstance(emails, list) and emails:
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), emails[0])
        if isinstance(primary, dict) and primary.get("value"):
            return str(primary["value"])
    return None


def _name_of(body: dict[str, Any]) -> str | None:
    name = body.get("name")
    if isinstance(name, dict):
        formatted = name.get("formatted")
        if formatted:
            return str(formatted)
        joined = " ".join(str(name.get(k) or "") for k in ("givenName", "familyName")).strip()
        if joined:
            return joined
    display = body.get("displayName")
    return str(display) if display else None


async def _guard(request: Request, work: Callable[[], Awaitable[Response]]) -> Response:
    try:
        return await work()
    except ScimError as exc:
        return scim_error(exc)
    except ScimNotFoundError as exc:
        return scim_error(ScimError(404, str(exc)))
    except ScimConflictError as exc:
        return scim_error(ScimError(409, str(exc), "uniqueness"))
    except MemberError as exc:
        return scim_error(ScimError(400, str(exc), "invalidValue"))


# --- discovery ---------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request) -> Response:
    async def work() -> Response:
        await _org(request)
        return _answer(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                "patch": {"supported": True},
                "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": MAX_RESULTS},
                "changePassword": {"supported": False},
                "sort": {"supported": False},
                "etag": {"supported": False},
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer token",
                        "description": "The token from Team → Provisioning",
                    }
                ],
            }
        )

    return await _guard(request, work)


@router.get("/ResourceTypes")
async def resource_types(request: Request) -> Response:
    async def work() -> Response:
        await _org(request)
        users = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": USER,
        }
        return _answer({"schemas": [LIST], "totalResults": 1, "Resources": [users]})

    return await _guard(request, work)


# --- users -----------------------------------------------------------------------------------


@router.get("/Users")
async def list_users(
    request: Request,
    filter_: str = Query(default="", alias="filter", max_length=300),
    start_index: int = Query(default=1, alias="startIndex"),
    count: int = 100,
) -> Response:
    async def work() -> Response:
        org = await _org(request)
        rows = await _service(request).users(org)
        if filter_:
            matched = _FILTER.match(filter_)
            if matched is None:
                raise ScimError(400, f"unsupported filter: {filter_[:100]}", "invalidFilter")
            attribute, value = matched.group(1).lower(), matched.group(2).lower()
            if attribute == "externalid":
                rows = [r for r in rows if (r.external_id or "").lower() == value]
            else:
                rows = [r for r in rows if r.email.lower() == value]
        start = max(1, start_index)
        page = rows[start - 1 : start - 1 + max(0, min(count, MAX_RESULTS))]
        return _answer(
            {
                "schemas": [LIST],
                "totalResults": len(rows),
                "startIndex": start,
                "itemsPerPage": len(page),
                "Resources": [_user(request, r) for r in page],
            }
        )

    return await _guard(request, work)


@router.get("/Users/{user_id}")
async def get_user(user_id: str, request: Request) -> Response:
    async def work() -> Response:
        org = await _org(request)
        return _answer(_user(request, await _service(request).user(org, _id(user_id))))

    return await _guard(request, work)


@router.post("/Users")
async def create_user(request: Request) -> Response:
    async def work() -> Response:
        org = await _org(request)
        body = await _body(request)
        email = str(body.get("userName") or _email_of(body) or "")
        if not email:
            raise ScimError(400, "userName is required", "invalidValue")
        row = await _service(request).create(
            org,
            email=email,
            name=_name_of(body) or "",
            active=_bool(body.get("active", True)),
            external_id=str(body["externalId"]) if body.get("externalId") else None,
        )
        return _answer(_user(request, row), status=201)

    return await _guard(request, work)


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request) -> Response:
    async def work() -> Response:
        org = await _org(request)
        body = await _body(request)
        changes = Changes(
            email=str(body.get("userName") or _email_of(body) or "") or None,
            name=_name_of(body),
            active=_bool(body["active"]) if "active" in body else None,
            external_id=str(body["externalId"]) if body.get("externalId") else None,
        )
        row = await _service(request).change(org, _id(user_id), changes)
        return _answer(_user(request, row))

    return await _guard(request, work)


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request) -> Response:
    async def work() -> Response:
        org = await _org(request)
        body = await _body(request)
        operations = body.get("Operations")
        if not isinstance(operations, list):
            raise ScimError(400, "Operations is required", "invalidSyntax")
        changes = _patch(operations)
        row = await _service(request).change(org, _id(user_id), changes)
        return _answer(_user(request, row))

    return await _guard(request, work)


@router.delete("/Users/{user_id}")
async def delete_user(user_id: str, request: Request) -> Response:
    async def work() -> Response:
        org = await _org(request)
        await _service(request).change(org, _id(user_id), Changes(active=False))
        return Response(status_code=204)

    return await _guard(request, work)


def _id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ScimError(404, f"no user {raw}") from exc


def _patch(operations: list[Any]) -> Changes:
    email: str | None = None
    name: str | None = None
    given: str | None = None
    family: str | None = None
    active: bool | None = None
    external: str | None = None
    for op in operations:
        if not isinstance(op, dict):
            raise ScimError(400, "an operation must be an object", "invalidSyntax")
        kind = str(op.get("op", "")).lower()
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"unsupported op {op.get('op')!r}", "invalidSyntax")
        path = str(op.get("path") or "")
        value = op.get("value")
        if kind == "remove":
            continue
        updates: dict[str, Any] = value if not path and isinstance(value, dict) else {path: value}
        for key, item in updates.items():
            key_l = key.lower()
            if key_l == "active":
                active = _bool(item)
            elif key_l == "username":
                email = str(item)
            elif key_l in ("displayname", "name.formatted"):
                name = str(item)
            elif key_l == "name.givenname":
                given = str(item)
            elif key_l == "name.familyname":
                family = str(item)
            elif key_l == "name" and isinstance(item, dict):
                name = _name_of({"name": item}) or name
            elif key_l == "externalid":
                external = str(item)
            elif key_l.startswith("emails") and email is None:
                found = _email_of({"emails": item}) if isinstance(item, list) else None
                email = found or (str(item) if isinstance(item, str) else None)
    if name is None and (given or family):
        name = " ".join(p for p in (given, family) if p)
    return Changes(email=email, name=name, active=active, external_id=external)
