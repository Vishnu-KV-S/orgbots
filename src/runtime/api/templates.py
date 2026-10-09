"""Bot templates over HTTP: export a bot, share it by link, and make a bot from one.

`/v1/bots/{bot_id}/template` is the bot as a template — the UI saves it as a file.
`/v1/bots/{bot_id}/template-links` are its share links: each a snapshot, listed until
turned off. `/v1/templates/preview` checks a template (a file someone opened) and says
what importing it will do; `/v1/templates/shared/{token}` does the same for a link;
`/v1/templates/import` makes the bot, from a file's template or a link's token, in the
caller's organization.

The token is the whole of a link's authority, so it is long and random, and a turned-off
link answers 410 with that reason rather than a 404 that reads like a typo. The rules
for what an import keeps — allow rules only when asked, routines paused — are in
`domain.templates`; nothing here relaxes them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _bot_view, _manager, _organization, _uow
from runtime.api.identity import current_member
from runtime.api.trail import audit
from runtime.domain.templates import BotTemplate, TemplateError, file_name, parse
from runtime.org.routines import describe
from runtime.org.templates import Preview, ShareRevokedError, TemplateService, preview
from runtime.persistence.repositories.templates import ShareRow

router = APIRouter(prefix="/v1/bots", tags=["bots"])
templates_router = APIRouter(prefix="/v1/templates", tags=["bots"])


def _service(request: Request) -> TemplateService:
    service = getattr(request.app.state, "templates", None)
    if service is None:
        service = TemplateService(_uow(request))
        request.app.state.templates = service
    return service


def _link_view(row: ShareRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "token": row.token,
        "name": str(row.template.get("name", "")),
        "uses": row.uses,
        "created_at": row.created_at.isoformat(),
    }


def _preview_view(shown: Preview) -> dict[str, Any]:
    plan = shown.plan
    return {
        "template": shown.template.model_dump(mode="json"),
        "file_name": file_name(shown.template),
        "plan": {
            "rules": [r.model_dump() for r in plan.rules],
            "allows": [r.model_dump() for r in plan.allows],
            "routines": [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "when": describe(r.cron, r.timezone)
                    if r.kind == "schedule"
                    else f"when a {r.source} event arrives",
                    "instruction": r.instruction,
                }
                for r in plan.routines
            ],
        },
    }


def _checked(template: BotTemplate | dict[str, Any], keep_allows: bool = False) -> Preview:
    try:
        parsed = template if isinstance(template, BotTemplate) else parse(template)
        return preview(parsed, keep_allows=keep_allows)
    except TemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _links_allowed(request: Request, organization_id: Any) -> bool:
    async with _uow(request)() as uow:
        return (await uow.policies.get(organization_id)).template_links


async def _shared(request: Request, token: str) -> tuple[ShareRow, BotTemplate]:
    try:
        found = await _service(request).shared(token)
    except ShareRevokedError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="no template at this link")
    if not await _links_allowed(request, found[0].organization_id):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="the organization that shared this turned its template links off",
        )
    return found


class PreviewBody(BaseModel):
    template: dict[str, Any]
    """As read from a file — checked by `domain.templates.parse`, which says what is
    wrong in words a person can act on."""


class ImportBody(BaseModel):
    template: dict[str, Any] | None = None
    token: str | None = Field(default=None, min_length=8, max_length=64)
    name: str | None = Field(default=None, max_length=80)
    keep_allows: bool = False
    """Write the template's allow rules too. The person saw them in the preview."""


# --- a bot's template and links ----------------------------------------------------------


@router.get("/{bot_id}/template")
async def export_template(bot_id: UUID, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    template = await _service(request).export(bot)
    return {"template": template.model_dump(mode="json"), "file_name": file_name(template)}


@router.get("/{bot_id}/template-links")
async def list_links(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    return {"links": [_link_view(r) for r in await _service(request).links(bot_id)]}


@router.post("/{bot_id}/template-links", status_code=status.HTTP_201_CREATED)
async def make_link(bot_id: UUID, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    if not await _links_allowed(request, bot.organization_id):
        raise HTTPException(
            status_code=403,
            detail="your organization's admins turned template links off; save a file instead",
        )
    try:
        row = await _service(request).share(bot)
    except TemplateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit(request, "template_link.made", bot.name, {"bot_id": str(bot.id)})
    return _link_view(row)


@router.delete("/{bot_id}/template-links/{link_id}")
async def revoke_link(bot_id: UUID, link_id: UUID, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    if not await _service(request).revoke(bot_id, link_id):
        raise HTTPException(status_code=404, detail="no such link")
    await audit(request, "template_link.revoked", bot.name, {"link_id": str(link_id)})
    return {"revoked": str(link_id)}


# --- making a bot from one ---------------------------------------------------------------


@templates_router.post("/preview")
async def preview_template(body: PreviewBody, request: Request) -> dict[str, Any]:
    await _organization(request)
    return _preview_view(_checked(body.template))


@templates_router.get("/shared/{token}")
async def shared_template(token: str, request: Request) -> dict[str, Any]:
    await _organization(request)
    row, template = await _shared(request, token)
    return {**_preview_view(_checked(template)), "uses": row.uses}


@templates_router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_template(body: ImportBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    if (body.template is None) == (body.token is None):
        raise HTTPException(status_code=422, detail="send a template or a link's token")
    share: ShareRow | None = None
    if body.token is not None:
        share, shared = await _shared(request, body.token)
        shown = _checked(shared, body.keep_allows)
    else:
        assert body.template is not None
        shown = _checked(body.template, body.keep_allows)
    try:
        member = await current_member(request)
        bot = await _manager(request).create_from_template(
            org,
            shown.template,
            name=body.name,
            keep_allows=body.keep_allows,
            owner_member_id=member.id if member else None,
        )
    except TemplateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if share is not None:
        async with _uow(request).transaction() as uow:
            await uow.template_shares.used(share.id)
    await audit(
        request,
        "bot.created",
        bot.name,
        {"bot_id": str(bot.id), "from": "link" if share else "template file"},
    )
    return _bot_view(bot)
