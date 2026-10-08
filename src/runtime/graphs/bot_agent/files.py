"""The team drive, as steps: `list_files`, `read_file`, `write_file`, `append_file`,
`edit_file`, `move_file` and `delete_file`. The rules are `domain.files`; the writes are
`org.files.TeamDrive`; this module turns a `BotStep` into one of them and says what
happened.

**What a bot has read is tracked per turn** (`files_read`: path → version, in graph
state and carried into a long task's next chunk). It is the `base_version` of the next
`write_file` over that path, which is how "read it before you replace it" and "it
changed after you read it" are enforced. A bot's own append or edit keeps it current
only if the bot had the latest version when it made the change: if a teammate's change
landed in between, the bot has not seen that change, and must read again before
replacing the file.

A read, a listing and a search go into the turn's results, beside helpers' answers and
recall, so the next step decides with the file in front of it. Everything else is one
line in the step log and one activity line in the conversation, which the person can
open the file from.
"""

from __future__ import annotations

import datetime as dt
import difflib
import uuid
from typing import Any

from runtime.domain.bots import BotStep
from runtime.domain.files import (
    Editor,
    FileError,
    NoSuchFileError,
    file_op_id,
    normalize_path,
    path_key,
    render_listing,
    render_read,
    render_search,
    size_label,
    team_of,
    window,
)


async def file_step(
    node: Any,
    bot: Any,
    step: BotStep,
    *,
    n: int,
    files_read: dict[str, int],
    say: Any,
) -> tuple[str, str | None]:
    """Do one file step. Returns the step-log line, and the result for the turn's
    results when there is one to read (a listing, a search, a file). `files_read` is updated
    in place."""
    drive = node.org.files
    run_id = node.ctx.run_id
    team = team_of(bot)
    now = dt.datetime.now(dt.UTC)
    raw = (step.path or "").strip()
    action: dict[str, Any] = {"type": step.action}
    if raw:
        action["path"] = raw

    async def said(ok: bool, **extra: Any) -> None:
        await say(step.action, "activity", step.thought, {"action": action, "ok": ok, **extra})

    try:
        if step.action == "list_files":
            query = (step.text or "").strip()
            if query:
                action["text"] = query
                hits = await drive.search(team, query)
                await said(True, found=len(hits))
                return (
                    f"searched the team drive for {query!r}: {len(hits)} file(s)",
                    render_search(query, hits, viewer=bot.id, now=now),
                )
            folder = normalize_path(raw, folder=True)
            action["path"] = folder
            files = await drive.listing(team, folder)
            await said(True, found=len(files))
            return (
                f"listed {folder}: {len(files)} file(s)",
                render_listing(files, folder=folder, viewer=bot.id, now=now),
            )

        if step.action == "read_file":
            found = await drive.find(team, raw)
            if found is None:
                raise NoSuchFileError(await _missing(drive, team, normalize_path(raw)))
            action["path"] = found.path
            win = window(found.content or "", step.from_line or 1)
            files_read[path_key(found.path)] = found.version
            await said(True, file_id=str(found.id), version=found.version)
            return (
                f"read {found.path} (v{found.version}, lines {win.first}-{win.last} of "
                f"{win.total})",
                render_read(found, win, viewer=bot.id, now=now),
            )

        editor = Editor("bot", bot.id, bot.name, uuid.UUID(str(run_id)))
        op_id = file_op_id(run_id, n)
        before = path_key(normalize_path(raw))
        try:
            if step.action == "write_file":
                change = await drive.write(
                    team,
                    raw,
                    step.text or "",
                    editor=editor,
                    op_id=op_id,
                    base_version=files_read.get(before, 0),
                )
            elif step.action == "append_file":
                change = await drive.append(team, raw, step.text or "", editor=editor, op_id=op_id)
            elif step.action == "edit_file":
                change = await drive.edit(
                    team, raw, step.find or "", step.text or "", editor=editor, op_id=op_id
                )
            elif step.action == "move_file":
                action["to"] = (step.to or "").strip()
                change = await drive.move(team, raw, step.to or "", editor=editor, op_id=op_id)
            else:
                change = await drive.delete(team, raw, editor=editor, op_id=op_id)
        except NoSuchFileError:
            raise NoSuchFileError(await _missing(drive, team, normalize_path(raw))) from None
    except FileError as exc:
        await said(False, error=str(exc))
        return f"{step.action} {raw or '/'} failed: {exc}", None

    f = change.file
    after = path_key(f.path)
    knew_latest = files_read.get(before) == change.previous_version
    if change.op == "delete":
        files_read.pop(before, None)
    elif change.op in ("create", "write"):
        files_read[after] = f.version
    elif knew_latest:
        files_read.pop(before, None)
        files_read[after] = f.version
    if change.op == "move":
        action["to"] = f.path
    else:
        action["path"] = f.path
    await said(True, file_id=str(f.id), version=f.version, chars=f.chars)
    return _line(change.op, raw, f), None


def _line(op: str, raw: str, f: Any) -> str:
    size = size_label(f.chars)
    if op == "create":
        return f"created {f.path} ({size})"
    if op == "write":
        return f"replaced {f.path} (now v{f.version}, {size})"
    if op == "append":
        return f"appended to {f.path} (now v{f.version}, {size})"
    if op == "edit":
        return f"edited {f.path} (now v{f.version}, {size})"
    if op == "move":
        return f"moved {normalize_path(raw)} to {f.path}"
    return f"deleted {f.path} (your person can restore it)"


async def _missing(drive: Any, team: Any, path: str) -> str:
    """No such file — and the names it was probably meant to be, so a typo costs the
    bot one step rather than two."""
    files = await drive.listing(team)
    by_key = {path_key(f.path): f.path for f in files}
    close = difflib.get_close_matches(path_key(path), list(by_key), n=3, cutoff=0.6)
    hint = f" Did you mean {' or '.join(by_key[c] for c in close)}?" if close else ""
    return f"there is no {path} in the team drive.{hint} list_files shows what is there."
