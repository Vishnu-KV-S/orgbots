"""A team's shared drive, as the graph and the API see it. The rules are `domain.files`.

One service for both callers, so a bot's write and a person's write are the same write
with a different `Editor`: the same lock, the same checks, the same revision. The
differences between them are explicit arguments rather than two code paths — a bot is
refused on a locked file and a person is not (`check_unlocked`), and a bot always says
which version it last read (`base_version`) while a person's editor may choose not to.

**Every change takes the team's lock first** (`TeamFileRepository.lock_team`), so a
check made inside the transaction — the path is free, this is the version the writer
read, there is room — is still true when the write lands.

**A change a run makes is idempotent per step.** It carries `op_id` (`file_op_id`), the
id of the revision it writes; a replayed step finds that revision already there and
gets back what it did the first time instead of doing it again.

Nothing here calls a model or leaves the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from runtime.domain.files import (
    MAX_REVISIONS,
    SEARCH_HITS,
    Editor,
    FilePathError,
    FileTakenError,
    NoSuchFileError,
    Team,
    appended,
    apply_edit,
    check_base,
    check_room,
    check_size,
    check_unlocked,
    move_target,
    normalize_path,
    path_key,
    score,
    snippet,
    terms,
)
from runtime.persistence.repositories.files import FileRevisionRow, TeamFileRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class Change:
    file: TeamFileRow
    op: str
    previous_version: int
    """The version the change was made on top of (0 for a new file). A writer that last
    saw exactly this version has seen everything but its own change."""
    replayed: bool = False


def clean_content(content: str) -> str:
    """Line endings as one character, and no NULs — Postgres text cannot hold one, and
    an edit's `find` should not have to guess which line ending a file uses."""
    return content.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


class TeamDrive:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    # --- reads -------------------------------------------------------------------

    async def summary(self, team: Team, limit: int) -> tuple[int, list[TeamFileRow]]:
        """How many files, and the most recently changed."""
        async with self._uow() as uow:
            return (
                await uow.files.count_live(team.team_id),
                await uow.files.recent(team.team_id, limit),
            )

    async def listing(self, team: Team, folder: str = "/") -> list[TeamFileRow]:
        folder = normalize_path(folder, folder=True)
        async with self._uow() as uow:
            return await uow.files.listing(team.team_id, folder)

    async def find(self, team: Team, path: str) -> TeamFileRow | None:
        async with self._uow() as uow:
            return await uow.files.live_by_path(team.team_id, normalize_path(path))

    async def get(self, team: Team, file_id: uuid.UUID) -> TeamFileRow | None:
        """A file by id — in the trash too — if it is this team's."""
        async with self._uow() as uow:
            row = await uow.files.get(file_id)
        return row if row is not None and row.team_id == team.team_id else None

    async def search(self, team: Team, query: str) -> list[tuple[TeamFileRow, str]]:
        wanted = terms(query)
        async with self._uow() as uow:
            candidates = await uow.files.search(team.team_id, wanted, limit=SEARCH_HITS * 4)
        ranked = sorted(
            ((f, score(f.path, f.content or "", wanted)) for f in candidates),
            key=lambda pair: (-pair[1], path_key(pair[0].path)),
        )
        return [(f, snippet(f.content or "", wanted)) for f, s in ranked[:SEARCH_HITS] if s > 0]

    async def trash(self, team: Team) -> list[TeamFileRow]:
        async with self._uow() as uow:
            return await uow.files.trash(team.team_id)

    async def revisions(self, team: Team, file_id: uuid.UUID) -> list[FileRevisionRow]:
        if await self.get(team, file_id) is None:
            raise NoSuchFileError(f"no file {file_id}")
        async with self._uow() as uow:
            return await uow.files.revisions(file_id, limit=MAX_REVISIONS)

    # --- changes -----------------------------------------------------------------

    async def write(
        self,
        team: Team,
        path: str,
        content: str,
        *,
        editor: Editor,
        op_id: uuid.UUID | None = None,
        base_version: int | None = None,
    ) -> Change:
        """Create a file, or replace one. `base_version`: see `check_base`."""
        path = normalize_path(path)
        content = clean_content(content)
        check_size(content)
        async with self._uow.transaction() as uow:
            done = await self._replayed(uow, op_id)
            if done is not None:
                return done
            await uow.files.lock_team(team.team_id)
            existing = await uow.files.live_by_path(team.team_id, path)
            check_base(existing, path, base_version, viewer=editor.bot_id, now=_now())
            if existing is None:
                return await self._create(uow, team, path, content, editor, op_id)
            check_unlocked(existing, editor)
            return await self._save(uow, existing, "write", existing.path, content, editor, op_id)

    async def create(self, team: Team, path: str, content: str, *, editor: Editor) -> Change:
        """A new file only — the person's "New file" and "Upload"."""
        path = normalize_path(path)
        content = clean_content(content)
        check_size(content)
        async with self._uow.transaction() as uow:
            await uow.files.lock_team(team.team_id)
            existing = await uow.files.live_by_path(team.team_id, path)
            if existing is not None:
                raise FileTakenError(f"{existing.path} already exists")
            return await self._create(uow, team, path, content, editor, None)

    async def append(
        self,
        team: Team,
        path: str,
        text: str,
        *,
        editor: Editor,
        op_id: uuid.UUID | None = None,
    ) -> Change:
        """Add to the end, creating the file if there is none — the way a log is kept."""
        path = normalize_path(path)
        text = clean_content(text)
        async with self._uow.transaction() as uow:
            done = await self._replayed(uow, op_id)
            if done is not None:
                return done
            await uow.files.lock_team(team.team_id)
            existing = await uow.files.live_by_path(team.team_id, path)
            if existing is None:
                check_size(text)
                return await self._create(uow, team, path, text, editor, op_id)
            check_unlocked(existing, editor)
            content = appended(existing.content or "", text)
            check_size(content)
            return await self._save(uow, existing, "append", existing.path, content, editor, op_id)

    async def edit(
        self,
        team: Team,
        path: str,
        find: str,
        replacement: str,
        *,
        editor: Editor,
        op_id: uuid.UUID | None = None,
    ) -> Change:
        path = normalize_path(path)
        async with self._uow.transaction() as uow:
            done = await self._replayed(uow, op_id)
            if done is not None:
                return done
            await uow.files.lock_team(team.team_id)
            existing = await self._existing(uow, team, path)
            check_unlocked(existing, editor)
            content = apply_edit(
                existing.content or "", clean_content(find), clean_content(replacement)
            )
            check_size(content)
            return await self._save(uow, existing, "edit", existing.path, content, editor, op_id)

    async def move(
        self,
        team: Team,
        path: str,
        to: str,
        *,
        editor: Editor,
        op_id: uuid.UUID | None = None,
    ) -> Change:
        """Rename, or move into another folder (`to` ending in `/`)."""
        path = normalize_path(path)
        target = move_target(path, to)
        async with self._uow.transaction() as uow:
            done = await self._replayed(uow, op_id)
            if done is not None:
                return done
            await uow.files.lock_team(team.team_id)
            existing = await self._existing(uow, team, path)
            check_unlocked(existing, editor)
            if target == existing.path:
                raise FilePathError(f"{existing.path} is already there")
            if path_key(target) != path_key(existing.path):
                taken = await uow.files.live_by_path(team.team_id, target)
                if taken is not None:
                    raise FileTakenError(
                        f"{taken.path} already exists; move it out of the way first, or "
                        "choose another name"
                    )
            return await self._save(
                uow,
                existing,
                "move",
                target,
                existing.content or "",
                editor,
                op_id,
                note=f"moved from {existing.path}",
            )

    async def delete(
        self,
        team: Team,
        path: str,
        *,
        editor: Editor,
        op_id: uuid.UUID | None = None,
        base_version: int | None = None,
    ) -> Change:
        """To the trash: the person can bring it back, with its history."""
        path = normalize_path(path)
        async with self._uow.transaction() as uow:
            done = await self._replayed(uow, op_id)
            if done is not None:
                return done
            await uow.files.lock_team(team.team_id)
            existing = await self._existing(uow, team, path)
            if base_version:
                check_base(existing, path, base_version, viewer=editor.bot_id, now=_now())
            check_unlocked(existing, editor)
            return await self._save(
                uow,
                existing,
                "delete",
                existing.path,
                existing.content or "",
                editor,
                op_id,
                deleted=True,
            )

    # --- the person's own changes, by id -------------------------------------------

    async def update(
        self,
        team: Team,
        file_id: uuid.UUID,
        *,
        editor: Editor,
        content: str | None = None,
        path: str | None = None,
        base_version: int | None = None,
    ) -> Change:
        """The file pane's Save or Rename — one at a time, because each is its own
        revision and a pair could half-happen. A save carries the version the person
        opened, so one made over a bot's newer change is refused rather than silently
        undoing it."""
        if (content is None) == (path is None):
            raise FilePathError("change either the content or the path")
        current = await self._own(team, file_id)
        if content is not None:
            return await self.write(
                team, current.path, content, editor=editor, base_version=base_version
            )
        assert path is not None
        return await self.move(team, current.path, path, editor=editor)

    async def remove(
        self, team: Team, file_id: uuid.UUID, *, editor: Editor, base_version: int | None = None
    ) -> Change:
        current = await self._own(team, file_id)
        return await self.delete(team, current.path, editor=editor, base_version=base_version)

    async def set_locked(self, team: Team, file_id: uuid.UUID, locked: bool) -> TeamFileRow:
        await self._own(team, file_id)
        async with self._uow.transaction() as uow:
            await uow.files.set_locked(file_id, locked)
            row = await uow.files.get(file_id)
        assert row is not None
        return row

    async def restore(
        self, team: Team, file_id: uuid.UUID, *, editor: Editor, version: int | None = None
    ) -> Change:
        """Bring a file back: a deleted one out of the trash, or any file to an earlier
        revision's content. Either way it is a new revision, so a restore is itself
        undoable."""
        async with self._uow.transaction() as uow:
            await uow.files.lock_team(team.team_id)
            current = await uow.files.get(file_id)
            if current is None or current.team_id != team.team_id:
                raise NoSuchFileError(f"no file {file_id}")
            content = current.content or ""
            note = "restored from the trash" if current.deleted_at else ""
            if version is None and current.deleted_at is None:
                raise FilePathError(f"{current.path} is not deleted; name a revision to restore")
            if version is not None:
                revision = await uow.files.revision_at(file_id, version)
                if revision is None:
                    raise NoSuchFileError(f"{current.path} has no revision {version} any more")
                content = revision.content
                note = f"restored revision {version}" + (" from the trash" if note else "")
            if current.deleted_at is not None:
                taken = await uow.files.live_by_path(team.team_id, current.path)
                if taken is not None:
                    raise FileTakenError(
                        f"{taken.path} has been created again since; rename it first"
                    )
                check_room(await uow.files.count_live(team.team_id))
            return await self._save(
                uow, current, "restore", current.path, content, editor, None, note=note
            )

    # --- internals ---------------------------------------------------------------

    async def _own(self, team: Team, file_id: uuid.UUID) -> TeamFileRow:
        row = await self.get(team, file_id)
        if row is None or row.deleted_at is not None:
            raise NoSuchFileError(f"no file {file_id}")
        return row

    @staticmethod
    async def _existing(uow: UnitOfWork, team: Team, path: str) -> TeamFileRow:
        existing = await uow.files.live_by_path(team.team_id, path)
        if existing is None:
            raise NoSuchFileError(
                f"there is no {path} in the team drive; list_files to see what is"
            )
        return existing

    @staticmethod
    async def _replayed(uow: UnitOfWork, op_id: uuid.UUID | None) -> Change | None:
        """The change a replayed step already made, as it was made."""
        if op_id is None:
            return None
        revision = await uow.files.revision(op_id)
        if revision is None:
            return None
        row = await uow.files.get(revision.file_id)
        assert row is not None
        return Change(row, revision.op, revision.version - 1, replayed=True)

    @staticmethod
    async def _create(
        uow: UnitOfWork,
        team: Team,
        path: str,
        content: str,
        editor: Editor,
        op_id: uuid.UUID | None,
    ) -> Change:
        check_room(await uow.files.count_live(team.team_id))
        file_id = uuid.uuid4()
        await uow.files.insert(
            file_id,
            organization_id=team.organization_id,
            team_id=team.team_id,
            path=path,
            content=content,
            editor_kind=editor.kind,
            editor_bot_id=editor.bot_id,
            editor_name=editor.name,
        )
        await uow.files.add_revision(
            op_id or uuid.uuid4(),
            file_id,
            version=1,
            op="create",
            path=path,
            content=content,
            editor_kind=editor.kind,
            editor_bot_id=editor.bot_id,
            editor_name=editor.name,
            run_id=editor.run_id,
        )
        row = await uow.files.get(file_id)
        assert row is not None
        return Change(row, "create", 0)

    @staticmethod
    async def _save(
        uow: UnitOfWork,
        existing: TeamFileRow,
        op: str,
        path: str,
        content: str,
        editor: Editor,
        op_id: uuid.UUID | None,
        *,
        note: str = "",
        deleted: bool = False,
    ) -> Change:
        version = existing.version + 1
        await uow.files.save(
            existing.id,
            path=path,
            content=content,
            version=version,
            editor_kind=editor.kind,
            editor_bot_id=editor.bot_id,
            editor_name=editor.name,
            deleted=deleted,
        )
        await uow.files.add_revision(
            op_id or uuid.uuid4(),
            existing.id,
            version=version,
            op=op,
            path=path,
            content=content,
            editor_kind=editor.kind,
            editor_bot_id=editor.bot_id,
            editor_name=editor.name,
            run_id=editor.run_id,
            note=note,
        )
        await uow.files.prune_revisions(existing.id, MAX_REVISIONS)
        row = await uow.files.get(existing.id)
        assert row is not None
        return Change(row, op, existing.version)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
