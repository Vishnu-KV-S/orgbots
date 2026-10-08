"""What a file is, and the text that can be read out of it.

Used where bytes enter a team drive — a person's attachment, a file a bot saved from the
web — so the drive stores, beside an image's or a PDF's bytes, everything a bot can
read without a vision model: a PDF's text page by page, a Word document's paragraphs, a
presentation's slide text. Extracted once, when the file is stored, and never again:
reading a file is then the same cheap `read_file` it is for a note.

**The bytes are untrusted, and so is this code's input.** Every format here is a
container a hostile sender controls, so each reader is bounded — pages, archive members,
decompressed size — and a file that cannot be read is stored with no text rather than
failing the upload. The type is decided by the bytes' own signature before the name or
the declared type, because both of those are whatever the sender typed.
"""

from __future__ import annotations

import io
import mimetypes
import re
import zipfile

from runtime.domain.files import MAX_FILE_CHARS, MAX_NAME_CHARS, TEXT_TYPES

PDF_PAGES = 200
ZIP_MEMBER_BYTES = 20 * 1024 * 1024
"""The most an Office document's XML may decompress to before reading stops — a
zip bomb is a few kilobytes that inflate to gigabytes."""

_OFFICE = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_TEXT_SUFFIX = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
}
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f:*?"<>|/\\]+')


def safe_name(name: str, fallback: str = "file") -> str:
    """A file name a drive path accepts: no folders, no characters paths refuse, at most
    `MAX_NAME_CHARS`, keeping the extension when it has to be shortened."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE_NAME.sub("-", base).strip(" .-") or fallback
    if len(base) <= MAX_NAME_CHARS:
        return base
    stem, dot, ext = base.rpartition(".")
    if dot and 0 < len(ext) <= 8:
        return stem[: MAX_NAME_CHARS - len(ext) - 1].rstrip(" .-") + "." + ext
    return base[:MAX_NAME_CHARS].rstrip(" .-")


def sniff(name: str, declared: str, data: bytes) -> str:
    """The media type, from the bytes first."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if head.startswith(b"PK\x03\x04"):
        return _OFFICE.get(suffix, "application/zip")
    if suffix in _TEXT_SUFFIX:
        return _TEXT_SUFFIX[suffix]
    if declared.startswith("text/") or declared in TEXT_TYPES:
        return declared
    guessed, _ = mimetypes.guess_type(name)
    if guessed and not guessed.startswith(("image/", "application/pdf")):
        return guessed
    return _looks_like_text(data) or "application/octet-stream"


def _looks_like_text(data: bytes) -> str | None:
    sample = data[:4096]
    if b"\x00" in sample:
        return None
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain"


def is_text(media_type: str) -> bool:
    return media_type in TEXT_TYPES or media_type.startswith("text/")


def extract_text(media_type: str, data: bytes) -> str:
    """The text in a binary file, or "" when there is none to read."""
    try:
        if media_type == "application/pdf":
            text = _pdf(data)
        elif "wordprocessingml" in media_type:
            text = _office(data, r"word/document\.xml", "w:p", "w:t")
        elif "presentationml" in media_type:
            text = _office(data, r"ppt/slides/slide\d+\.xml", "a:p", "a:t")
        elif "spreadsheetml" in media_type:
            text = _office(data, r"xl/sharedStrings\.xml", "si", "t")
        else:
            return ""
    except Exception:
        # A malformed or hostile file stores with no text; it is still the person's file.
        return ""
    text = text.replace("\x00", "").strip()
    return text[:MAX_FILE_CHARS]


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    total = 0
    for number, page in enumerate(reader.pages[:PDF_PAGES], 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[page {number}]\n{text}")
            total += len(text)
        if total > MAX_FILE_CHARS:
            break
    return "\n\n".join(pages)


def _office(data: bytes, member: str, block: str, run: str) -> str:
    """Paragraph text out of Office Open XML: `run` elements' text, one line per `block`."""
    wanted = re.compile(member)
    paragraph = re.compile(rf"<{block}[ >].*?</{block}>|<{block}/>", re.S)
    piece = re.compile(rf"<{run}(?: [^>]*)?>([^<]*)</{run}>")
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted((n for n in archive.namelist() if wanted.fullmatch(n)), key=_natural)
        budget = ZIP_MEMBER_BYTES
        for name in names:
            info = archive.getinfo(name)
            if info.file_size > budget:
                break
            budget -= info.file_size
            xml = archive.read(name).decode("utf-8", errors="replace")
            for para in paragraph.findall(xml):
                line = "".join(piece.findall(para))
                if line.strip():
                    out.append(_unescape(line))
    return "\n".join(out)


def _natural(name: str) -> list[object]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )
