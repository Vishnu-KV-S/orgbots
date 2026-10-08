"""`/v1/connectors` — the organization's apps: MCP servers its bots can call.

Adding one **connects to it first**: `initialize` and `tools/list` over
`runtime.gateway.mcp`, with the token the person typed. A server that cannot be reached,
refuses the token, or lists no tools is not saved — the person sees why and fixes the
address or the token, rather than a bot finding out mid-task. What is saved is the
address, the auth kind, the token sealed under the credential cipher, and the tool list
a bot's prompt names. *Refresh* reconnects (with the stored token) and replaces the
list, for a server that added tools.

No response carries a token, only whether one is set. The marketplace's connectors are
`/v1/marketplace/connectors/{key}`: the catalog entry's address and auth, and the
person's token when it needs one.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

from runtime.api.bots import _organization, _settings, _uow
from runtime.api.identity import current_member
from runtime.api.trail import audit
from runtime.domain.connectors import MAX_CONNECTORS, ConnectorError, connector_name
from runtime.domain.policies import Policy
from runtime.gateway.builtin.connectors import open_auth, token_aad
from runtime.gateway.mcp import Auth, Connect, MCPError, connect
from runtime.gateway.vault import VaultUnavailableError, load_cipher
from runtime.org.marketplace import CONNECTORS, catalog_connector
from runtime.persistence.repositories.connectors import ConnectorRow

router = APIRouter(prefix="/v1/connectors", tags=["connectors"])
marketplace_router = APIRouter(prefix="/v1/marketplace", tags=["connectors"])


def _connect(request: Request) -> Connect:
    return getattr(request.app.state, "mcp_connect", None) or connect


def _view(row: ConnectorRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "title": row.title,
        "url": row.url,
        "auth_kind": row.auth_kind,
        "header_name": row.header_name,
        "has_token": row.has_secret,
        "server_name": row.server_name,
        "status": row.status,
        "last_error": row.last_error,
        "enabled": row.enabled,
        "catalog_key": row.catalog_key,
        "tools": [
            {
                "name": t.get("name"),
                "description": " ".join(str(t.get("description") or "").split())[:300],
                "read_only": bool((t.get("annotations") or {}).get("readOnlyHint")),
            }
            for t in row.tools
        ],
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def _probe(request: Request, url: str, auth: Auth) -> tuple[str, list[dict[str, Any]]]:
    try:
        async with _connect(request)(url, auth) as session:
            tools = await session.list_tools()
            server = str(session.server.get("name") or "")
    except MCPError as exc:
        raise HTTPException(status_code=422, detail=f"could not connect: {exc}") from exc
    if not tools:
        raise HTTPException(status_code=422, detail="the server lists no tools")
    return server, tools


def _seal(request: Request, row_like: Any, token: str) -> dict[str, Any]:
    try:
        cipher = load_cipher(_settings(request))
    except VaultUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc
    key_id, nonce, ciphertext = cipher.encrypt(token, aad=token_aad(row_like))
    return {"secret_key_id": key_id, "secret_nonce": nonce, "secret_ciphertext": ciphertext}


class _Ref:
    def __init__(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        self.organization_id = organization_id
        self.id = connector_id


async def _may_manage(request: Request) -> Policy:
    """Apps are the organization's, and their tokens are: with members, owners and
    admins connect and change them, unless the policy lets members too."""
    org = await _organization(request)
    async with _uow(request)() as uow:
        policy = await uow.policies.get(org)
    member = await current_member(request)
    if member is not None and not member.is_admin and not policy.members_add_apps:
        raise HTTPException(
            status_code=403,
            detail="your organization's admins connect apps; ask one of them",
        )
    return policy


async def _add(
    request: Request,
    *,
    title: str,
    url: str,
    name: str,
    auth_kind: str,
    header_name: str,
    token: str,
    catalog_key: str | None,
) -> ConnectorRow:
    org = await _organization(request)
    policy = await _may_manage(request)
    try:
        slug = connector_name(name or title)
    except ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="the address must start with https://")
    if not policy.url_allowed(url):
        raise HTTPException(
            status_code=422,
            detail="your organization's network allowlist does not include "
            f"{urlparse(url).hostname}",
        )
    async with _uow(request)() as uow:
        if await uow.connectors.by_name(org, slug) is not None:
            raise HTTPException(status_code=409, detail=f"there is already a connector {slug!r}")
        if len(await uow.connectors.for_organization(org)) >= MAX_CONNECTORS:
            raise HTTPException(status_code=409, detail=f"at most {MAX_CONNECTORS} connectors")
    kind = auth_kind if token else "none"
    server, tools = await _probe(request, url, Auth(kind=kind, header=header_name, token=token))
    connector_id = uuid.uuid4()
    async with _uow(request).transaction() as uow:
        await uow.connectors.create(
            connector_id,
            org,
            name=slug,
            title=title.strip() or slug,
            url=url.strip(),
            auth_kind=kind,
            header_name=header_name.strip(),
            catalog_key=catalog_key,
        )
        fields: dict[str, Any] = {"tools": tools, "server_name": server}
        if token:
            fields.update(_seal(request, _Ref(org, connector_id), token))
        await uow.connectors.update(connector_id, fields)
        row = await uow.connectors.get(connector_id)
    assert row is not None
    await audit(request, "connector.added", slug, {"url": row.url, "catalog_key": catalog_key})
    return row


async def _own(request: Request, connector_id: UUID) -> ConnectorRow:
    org = await _organization(request)
    async with _uow(request)() as uow:
        row = await uow.connectors.get(connector_id)
    if row is None or row.organization_id != org:
        raise HTTPException(status_code=404, detail=f"no connector {connector_id}")
    return row


class AddBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=500)
    name: str = Field(default="", max_length=40)
    auth_kind: str = Field(default="none", pattern="^(none|bearer|header)$")
    header_name: str = Field(default="", max_length=80)
    token: SecretStr | None = Field(default=None, max_length=4_000)


class PatchBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    token: SecretStr | None = Field(default=None, max_length=4_000)


class InstallBody(BaseModel):
    token: SecretStr | None = Field(default=None, max_length=4_000)


@router.get("")
async def list_connectors(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    async with _uow(request)() as uow:
        rows = await uow.connectors.for_organization(org)
    return {"connectors": [_view(r) for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_connector(body: AddBody, request: Request) -> dict[str, Any]:
    row = await _add(
        request,
        title=body.title,
        url=body.url,
        name=body.name,
        auth_kind=body.auth_kind,
        header_name=body.header_name,
        token=body.token.get_secret_value() if body.token else "",
        catalog_key=None,
    )
    return _view(row)


@router.post("/{connector_id}/refresh")
async def refresh_connector(connector_id: UUID, request: Request) -> dict[str, Any]:
    row = await _own(request, connector_id)
    await _may_manage(request)
    try:
        auth = open_auth(row, _settings(request))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="the stored token could not be opened") from exc
    try:
        async with _connect(request)(row.url, auth) as session:
            tools = await session.list_tools()
            server = str(session.server.get("name") or row.server_name)
        fields: dict[str, Any] = {
            "tools": tools,
            "server_name": server,
            "status": "ok",
            "last_error": "",
        }
    except MCPError as exc:
        fields = {"status": "error", "last_error": str(exc)[:300]}
    async with _uow(request).transaction() as uow:
        await uow.connectors.update(row.id, fields)
        fresh = await uow.connectors.get(row.id)
    assert fresh is not None
    return _view(fresh)


@router.patch("/{connector_id}")
async def update_connector(connector_id: UUID, body: PatchBody, request: Request) -> dict[str, Any]:
    row = await _own(request, connector_id)
    await _may_manage(request)
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.token is not None:
        token = body.token.get_secret_value()
        if token:
            kind = row.auth_kind if row.auth_kind != "none" else "bearer"
            await _probe(request, row.url, Auth(kind=kind, header=row.header_name, token=token))
            fields.update(_seal(request, row, token), auth_kind=kind, status="ok", last_error="")
        else:
            fields.update(
                secret_key_id=None, secret_nonce=None, secret_ciphertext=None, auth_kind="none"
            )
    async with _uow(request).transaction() as uow:
        await uow.connectors.update(row.id, fields)
        fresh = await uow.connectors.get(row.id)
    assert fresh is not None
    return _view(fresh)


@router.delete("/{connector_id}")
async def delete_connector(connector_id: UUID, request: Request) -> dict[str, Any]:
    row = await _own(request, connector_id)
    await _may_manage(request)
    async with _uow(request).transaction() as uow:
        await uow.connectors.soft_delete(connector_id)
    await audit(request, "connector.removed", row.name)
    return {"deleted": str(connector_id)}


@marketplace_router.get("/connectors")
async def marketplace_connectors(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    async with _uow(request)() as uow:
        installed = {r.catalog_key for r in await uow.connectors.for_organization(org)}
    return {
        "connectors": [
            {
                "key": c.key,
                "title": c.title,
                "category": c.category,
                "blurb": c.blurb,
                "url": c.url,
                "auth_kind": c.auth_kind,
                "key_help": c.key_help,
                "needs_token": c.auth_kind != "none" and not c.optional_token,
                "takes_token": c.auth_kind != "none",
                "checked": c.checked,
                "installed": c.key in installed,
            }
            for c in CONNECTORS
        ]
    }


@marketplace_router.post("/connectors/{key}", status_code=status.HTTP_201_CREATED)
async def install_connector(key: str, body: InstallBody, request: Request) -> dict[str, Any]:
    item = catalog_connector(key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no connector {key!r} in the marketplace")
    token = body.token.get_secret_value() if body.token else ""
    if item.auth_kind != "none" and not item.optional_token and not token:
        raise HTTPException(status_code=422, detail=f"{item.title} needs a token: {item.key_help}")
    row = await _add(
        request,
        title=item.title,
        url=item.url,
        name=item.key,
        auth_kind=item.auth_kind,
        header_name=item.header_name,
        token=token,
        catalog_key=item.key,
    )
    return _view(row)
