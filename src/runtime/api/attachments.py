"""Bytes into a team's drive, and back out.

`POST /v1/bots/{bot_id}/files/upload` stores files a person brings in — a message's
attachments (into `/attachments/<date>/` by default) or an upload in the Files pane —
through `TeamDrive.upload`, which names them freely, never over a teammate's file.
The body is JSON with base64 content, not multipart: the UI reaches the API through a
proxy that passes bodies through as text, and a multipart body of a PDF would not
survive that.

`GET /v1/bots/{bot_id}/files/{file_id}/raw` returns a file's bytes — an image the chat
shows, a PDF the person downloads — with the stored media type, as an attachment
download unless it is an image, so a stored HTML file is never rendered inline as a
page of this origin.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _drive, _file_error, _file_view
from runtime.domain.files import (
    IMAGE_TYPES,
    MAX_ATTACHMENTS,
    MAX_BLOB_BYTES,
    PERSON,
    FileError,
    name_of,
    team_of,
)
from runtime.org.extract import sniff

router = APIRouter(prefix="/v1/bots", tags=["bots"])


class UploadFile(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="", max_length=120)
    data: str = Field(max_length=(MAX_BLOB_BYTES * 4) // 3 + 16, repr=False)
    """The file's bytes, base64."""


class UploadBody(BaseModel):
    files: list[UploadFile] = Field(min_length=1, max_length=MAX_ATTACHMENTS)
    folder: str | None = Field(default=None, max_length=200)
    """Where to put them. Default: `/attachments/<today>`."""


@router.post("/{bot_id}/files/upload", status_code=status.HTTP_201_CREATED)
async def upload(bot_id: UUID, body: UploadBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    team = team_of(bot)
    folder = body.folder or f"/attachments/{dt.datetime.now(dt.UTC).date().isoformat()}"
    drive = _drive(request)
    stored: list[dict[str, Any]] = []
    for item in body.files:
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"{item.name}: not base64") from exc
        media_type = sniff(item.name, item.media_type, data)
        try:
            change = await drive.upload(team, folder, item.name, data, media_type, editor=PERSON)
        except FileError as exc:
            raise _file_error(exc) from exc
        stored.append(_file_view(change.file))
    return {"files": stored}


@router.get("/{bot_id}/files/{file_id}/raw")
async def raw(bot_id: UUID, file_id: UUID, request: Request) -> Response:
    bot = await _bot_or_404(request, bot_id)
    try:
        row, data = await _drive(request).blob(team_of(bot), file_id)
    except FileError as exc:
        raise _file_error(exc) from exc
    inline = row.media_type in IMAGE_TYPES or row.media_type == "application/pdf"
    disposition = "inline" if inline else "attachment"
    filename = quote(name_of(row.path))
    return Response(
        content=data,
        media_type=row.media_type if row.blob_sha else "text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
