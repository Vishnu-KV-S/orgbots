"""Attachments: images, PDFs and documents in messages and in a team's drive.

What a file is and what text it holds needs nothing running; the drive and the API
need Postgres (`runtime_features_test`, never the dev database); the graph's `look` at
a file runs on the fakes from `test_bots.py`.
"""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.files import (
    BinaryFileError,
    Editor,
    Team,
    render_attachments,
)
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.extract import extract_text, safe_name, sniff
from runtime.org.files import TeamDrive
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99"
    b"=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pdf_with(text: str) -> bytes:
    """A one-page PDF whose text a reader can extract — built by hand, offsets and all."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def docx_with(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
    return out.getvalue()


# --- what a file is ------------------------------------------------------------------------


def test_the_bytes_decide_the_type_not_the_name() -> None:
    assert sniff("notes.txt", "text/plain", pdf_with("x")) == "application/pdf"
    assert sniff("photo", "", PNG) == "image/png"
    assert sniff("report.docx", "application/octet-stream", docx_with("a")).endswith(
        "wordprocessingml.document"
    )
    assert sniff("data.csv", "", b"a,b\n1,2\n") == "text/csv"
    assert sniff("blob.bin", "", b"\x00\x01\x02binary") == "application/octet-stream"
    assert sniff("readme", "", "plain words".encode()) == "text/plain"


def test_text_comes_out_of_a_pdf_and_a_word_document() -> None:
    assert "Invoice 42 total 99.50" in extract_text("application/pdf", pdf_with("Invoice 42 total 99.50"))
    word = extract_text(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        docx_with("First &amp; foremost", "Second"),
    )
    assert word == "First & foremost\nSecond"
    assert extract_text("application/pdf", b"%PDF-1.4 broken") == ""
    assert extract_text("image/png", PNG) == ""


def test_a_name_becomes_one_a_path_accepts() -> None:
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name('in"voice:<1>.pdf') == "in-voice-1-.pdf"
    long = safe_name("x" * 120 + ".pdf")
    assert len(long) == 80 and long.endswith(".pdf")
    assert safe_name("   ") == "file"


def test_attachments_read_as_where_they_are_and_how_to_read_them() -> None:
    text = render_attachments(
        [
            {"path": "/attachments/2026-10-09/a.pdf", "kind": "PDF", "chars": 900},
            {"path": "/attachments/2026-10-09/b.png", "kind": "image"},
            {"path": "/attachments/2026-10-09/c.csv", "kind": "text"},
        ]
    )
    assert "a.pdf (PDF, read_file reads its text)" in text
    assert "b.png (look at it with look, path)" in text and "c.csv (read_file it)" in text


# --- the drive -----------------------------------------------------------------------------


async def _team(uow_factory: Any, organization_id: Any) -> Team:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "attachments")
    return Team(organization_id=organization_id, team_id=uuid.uuid4())


PERSON = Editor("person", None, "you")
BOT = Editor("bot", uuid.uuid4(), "Scout")


async def test_a_pdf_is_stored_once_read_as_text_and_never_edited_as_text(
    uow_factory: Any, organization_id: Any
) -> None:
    team = await _team(uow_factory, organization_id)
    drive = TeamDrive(uow_factory)
    data = pdf_with("Invoice 42 total 99.50")

    first = await drive.upload(team, "/attachments", "invoice.pdf", data, "application/pdf",
                               editor=PERSON)
    again = await drive.upload(team, "/attachments", "invoice.pdf", data, "application/pdf",
                               editor=PERSON)
    assert first.file.path == "/attachments/invoice.pdf"
    assert again.file.path == "/attachments/invoice-2.pdf", "never over a teammate's file"
    assert first.file.blob_sha == again.file.blob_sha and first.file.bytes == len(data)
    assert "Invoice 42" in (first.file.content or "")

    row, stored = await drive.blob(team, first.file.id)
    assert stored == data and row.media_type == "application/pdf"

    for attempt in (
        drive.write(team, row.path, "x", editor=BOT, base_version=1),
        drive.append(team, row.path, "x", editor=BOT),
        drive.edit(team, row.path, "Invoice", "Receipt", editor=BOT),
    ):
        with pytest.raises(BinaryFileError, match="is a PDF"):
            await attempt

    moved = await drive.move(team, row.path, "/finance/", editor=BOT)
    assert moved.file.path == "/finance/invoice.pdf" and moved.file.blob_sha == row.blob_sha
    gone = await drive.delete(team, moved.file.path, editor=BOT)
    back = await drive.restore(team, gone.file.id, editor=PERSON)
    assert back.file.blob_sha == row.blob_sha and back.file.bytes == len(data)
    _, still = await drive.blob(team, back.file.id)
    assert still == data

    text = await drive.upload(team, "/", "notes.md", b"# Hi\r\n", "text/markdown", editor=PERSON)
    assert text.file.blob_sha is None and text.file.content == "# Hi\n"
    edited = await drive.edit(team, "/notes.md", "Hi", "Hello", editor=BOT)
    assert edited.file.content == "# Hello\n"


async def test_reading_a_binary_file_says_what_it_is(uow_factory: Any, organization_id: Any) -> None:
    import datetime as dt

    from runtime.domain.files import render_read, window

    team = await _team(uow_factory, organization_id)
    drive = TeamDrive(uow_factory)
    png = await drive.upload(team, "/", "chart.png", PNG, "image/png", editor=PERSON)
    pdf = await drive.upload(team, "/", "a.pdf", pdf_with("Quarterly"), "application/pdf",
                             editor=PERSON)
    now = dt.datetime.now(dt.UTC)
    image = render_read(png.file, window(png.file.content or ""), viewer=None, now=now)
    assert "image" in image and "look at it with look, path = /chart.png" in image
    doc = render_read(pdf.file, window(pdf.file.content or ""), viewer=None, now=now)
    assert "the text read out of it" in doc and "Quarterly" in doc


# --- the graph -----------------------------------------------------------------------------


@dataclass
class _Row:
    path: str
    media_type: str
    blob_sha: str | None = "sha"

    @property
    def is_binary(self) -> bool:
        return self.blob_sha is not None


@dataclass
class FakeDrive:
    files: dict[str, tuple[_Row, bytes]] = field(default_factory=dict)

    async def summary(self, team: Any, limit: int) -> tuple[int, list[Any]]:
        return len(self.files), []

    async def blob_at(self, team: Any, path: str) -> tuple[_Row, bytes]:
        from runtime.domain.files import NoSuchFileError

        if path not in self.files:
            raise NoSuchFileError(f"there is no {path} in the team drive")
        return self.files[path]


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


async def test_a_bot_sees_its_persons_attachments_and_looks_at_an_image() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    await bots.record(
        bot.id, run_id="r0", step=0, kind="m", role="user", content="What's on this receipt?",
        payload={"attachments": [{"path": "/attachments/r.png", "kind": "image"}]},
    )
    drive = FakeDrive({"/attachments/r.png": (_Row("/attachments/r.png", "image/png"), PNG),
                       "/notes.md": (_Row("/notes.md", "text/plain", None), b"x")})
    look = {"thought": "Read it", "action": "look", "text": "What is the total?",
            "path": "/attachments/r.png"}
    seen = {"answer": "Total: 42.00 EUR", "elements": [], "captcha": False}
    wrong = {"thought": "And this", "action": "look", "text": "?", "path": "/notes.md"}
    reply = {"thought": "Done", "action": "reply", "text": "The total is 42.00 EUR."}
    model = ScriptedModel([look, seen, wrong, reply])
    gateway = FakePageGateway()
    out = await _invoke(_Node(_Ctx(), gateway, model, _Org(bots, files=drive)))

    assert "(attached: /attachments/r.png (look at it with look, path) — to give one to a website, upload it with its path)" in model.prompts[0]  # noqa: E501
    assert "The image is the file /attachments/r.png" in model.prompts[1]
    assert "Total: 42.00 EUR" in model.prompts[2]
    assert "is a text file, not an image" in model.prompts[3]
    assert out["reply"] == "The total is 42.00 EUR."
    assert not any(c.get("screenshot") for c in gateway.calls), "a file look takes no screenshot"


# --- the API -------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any
) -> AsyncIterator[httpx.AsyncClient]:
    from runtime.runtime.run_service import RunService

    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    app.state.service = RunService(uow_factory, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http


async def test_a_message_carries_attachments_the_drive_now_holds(
    api: httpx.AsyncClient, uow_factory: Any
) -> None:
    bot = (await api.post("/v1/bots", json={"name": "Clerk"})).json()
    up = await api.post(
        f"/v1/bots/{bot['id']}/files/upload",
        json={"files": [
            {"name": "receipt.png", "media_type": "image/png",
             "data": base64.b64encode(PNG).decode()},
            {"name": "inv.pdf", "data": base64.b64encode(pdf_with("Total 12")).decode()},
        ]},
    )
    assert up.status_code == 201, up.text
    png, pdf = up.json()["files"]
    assert png["path"].startswith("/attachments/") and png["kind"] == "image"
    assert pdf["media_type"] == "application/pdf" and pdf["binary"] is True

    raw = await api.get(f"/v1/bots/{bot['id']}/files/{png['id']}/raw")
    assert raw.content == PNG and raw.headers["content-type"] == "image/png"
    assert raw.headers["x-content-type-options"] == "nosniff"

    sent = await api.post(
        f"/v1/bots/{bot['id']}/messages", json={"text": "", "attachments": [png["id"], pdf["id"]]}
    )
    assert sent.status_code == 202, sent.text
    assert (await api.post(f"/v1/bots/{bot['id']}/messages", json={"text": " "})).status_code == 422
    stranger = await api.post(
        f"/v1/bots/{bot['id']}/messages", json={"text": "x", "attachments": [str(uuid.uuid4())]}
    )
    assert stranger.status_code == 422

    async with uow_factory() as uow:
        (said,) = [m for m in await uow.bots.messages(uuid.UUID(bot["id"]), after_seq=0)
                   if m.role == "user"]
    names = [a["name"] for a in said.payload["attachments"]]
    assert names == ["receipt.png", "inv.pdf"]
    assert said.payload["attachments"][1]["kind"] == "PDF"
    assert said.payload["attachments"][1]["chars"] > 0

    bad = await api.post(f"/v1/bots/{bot['id']}/files/upload",
                         json={"files": [{"name": "x", "data": "@@@"}]})
    assert bad.status_code == 422
