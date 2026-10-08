"""Team drives: files and their revisions. See migration 042 and `domain.files`.

Every write the drive makes takes `lock_team` first, in the same transaction, so the
checks the service makes before writing — is the path free, is this the version the
writer read, is there room — cannot be raced by a teammate writing at the same moment.
A team's writes are a handful a minute; serialising them costs nothing anyone notices,
and it is what makes the checks true rather than probably true.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TEAM_DRIVE_LOCK_NAMESPACE = 0x7EF1
"""Advisory-lock namespace for team drives. Distinct from M4's apply lock and the test
suite's session lock."""

_META = """
    id, organization_id, team_id, path, chars, version, locked, created_by_kind,
    created_by_bot_id, created_by_name, updated_by_kind, updated_by_bot_id, updated_by_name,
    created_at, updated_at, deleted_at
"""
_FULL = _META + ", content"

_REVISION = """
    id, file_id, version, op, path, content, editor_kind, editor_bot_id, editor_name,
    run_id, note, created_at
"""


def team_lock_key(team_id: uuid.UUID) -> int:
    """A signed 64-bit key for `pg_advisory_xact_lock`, folded from the team id the
    same way `spec.apply_lock_key` folds an organization's."""
    folded = (TEAM_DRIVE_LOCK_NAMESPACE << 48) ^ (team_id.int & ((1 << 48) - 1))
    return folded - (1 << 63) if folded >= (1 << 63) else folded


@dataclass(frozen=True, slots=True)
class TeamFileRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    team_id: uuid.UUID
    path: str
    chars: int
    version: int
    locked: bool
    created_by_kind: str
    created_by_bot_id: uuid.UUID | None
    created_by_name: str
    updated_by_kind: str
    updated_by_bot_id: uuid.UUID | None
    updated_by_name: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    content: str | None
    """`None` when the query was a listing; listings never read file bodies."""


@dataclass(frozen=True, slots=True)
class FileRevisionRow:
    id: uuid.UUID
    file_id: uuid.UUID
    version: int
    op: str
    path: str
    content: str
    editor_kind: str
    editor_bot_id: uuid.UUID | None
    editor_name: str
    run_id: uuid.UUID | None
    note: str
    created_at: datetime


def _file(r: Any) -> TeamFileRow:
    return TeamFileRow(
        id=r.id,
        organization_id=r.organization_id,
        team_id=r.team_id,
        path=r.path,
        chars=int(r.chars),
        version=int(r.version),
        locked=bool(r.locked),
        created_by_kind=r.created_by_kind,
        created_by_bot_id=r.created_by_bot_id,
        created_by_name=r.created_by_name,
        updated_by_kind=r.updated_by_kind,
        updated_by_bot_id=r.updated_by_bot_id,
        updated_by_name=r.updated_by_name,
        created_at=r.created_at,
        updated_at=r.updated_at,
        deleted_at=r.deleted_at,
        content=getattr(r, "content", None),
    )


def _revision(r: Any) -> FileRevisionRow:
    return FileRevisionRow(
        id=r.id,
        file_id=r.file_id,
        version=int(r.version),
        op=r.op,
        path=r.path,
        content=r.content,
        editor_kind=r.editor_kind,
        editor_bot_id=r.editor_bot_id,
        editor_name=r.editor_name,
        run_id=r.run_id,
        note=r.note,
        created_at=r.created_at,
    )


class TeamFileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def lock_team(self, team_id: uuid.UUID) -> None:
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": team_lock_key(team_id)}
        )

    # --- reads -------------------------------------------------------------------

    async def count_live(self, team_id: uuid.UUID) -> int:
        row = (
            await self._s.execute(
                text(
                    "SELECT count(*) AS n FROM team_files WHERE team_id = :t AND deleted_at IS NULL"
                ),
                {"t": team_id},
            )
        ).one()
        return int(row.n)

    async def live_by_path(self, team_id: uuid.UUID, path: str) -> TeamFileRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_FULL} FROM team_files WHERE team_id = :t "
                    "AND lower(path) = lower(:p) AND deleted_at IS NULL"
                ),
                {"t": team_id, "p": path},
            )
        ).one_or_none()
        return None if row is None else _file(row)

    async def get(self, file_id: uuid.UUID) -> TeamFileRow | None:
        """A file by id, deleted or not, with its content."""
        row = (
            await self._s.execute(
                text(f"SELECT {_FULL} FROM team_files WHERE id = :id"), {"id": file_id}
            )
        ).one_or_none()
        return None if row is None else _file(row)

    async def listing(self, team_id: uuid.UUID, folder: str = "/") -> list[TeamFileRow]:
        """Live files under `folder` at any depth, without their content."""
        prefix = "" if folder == "/" else folder.lower() + "/"
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_META} FROM team_files WHERE team_id = :t AND deleted_at IS NULL "
                    "AND left(lower(path), :n) = :prefix ORDER BY lower(path)"
                ),
                {"t": team_id, "n": len(prefix), "prefix": prefix},
            )
        ).all()
        return [_file(r) for r in rows]

    async def recent(self, team_id: uuid.UUID, limit: int) -> list[TeamFileRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_META} FROM team_files WHERE team_id = :t AND deleted_at IS NULL "
                    "ORDER BY updated_at DESC, lower(path) LIMIT :limit"
                ),
                {"t": team_id, "limit": limit},
            )
        ).all()
        return [_file(r) for r in rows]

    async def search(self, team_id: uuid.UUID, terms: list[str], limit: int) -> list[TeamFileRow]:
        """Live files whose path or content contains any of `terms`, with content, for
        the service to rank. Any rather than all: ranking puts the files that match
        more of the words first, and a search that found nothing because one word was
        spelled differently is a worse answer than a long list."""
        if not terms:
            return []
        clauses = " OR ".join(
            f"strpos(lower(path), :t{i}) > 0 OR strpos(lower(content), :t{i}) > 0"
            for i in range(len(terms))
        )
        params: dict[str, Any] = {f"t{i}": t for i, t in enumerate(terms)}
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_FULL} FROM team_files WHERE team_id = :team "
                    f"AND deleted_at IS NULL AND ({clauses}) ORDER BY updated_at DESC LIMIT :limit"
                ),
                {"team": team_id, "limit": limit, **params},
            )
        ).all()
        return [_file(r) for r in rows]

    async def trash(self, team_id: uuid.UUID, limit: int = 50) -> list[TeamFileRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_META} FROM team_files WHERE team_id = :t AND deleted_at IS NOT NULL "
                    "ORDER BY deleted_at DESC LIMIT :limit"
                ),
                {"t": team_id, "limit": limit},
            )
        ).all()
        return [_file(r) for r in rows]

    # --- writes ------------------------------------------------------------------

    async def insert(
        self,
        file_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        team_id: uuid.UUID,
        path: str,
        content: str,
        editor_kind: str,
        editor_bot_id: uuid.UUID | None,
        editor_name: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO team_files (id, organization_id, team_id, path, content, chars,
                                        created_by_kind, created_by_bot_id, created_by_name,
                                        updated_by_kind, updated_by_bot_id, updated_by_name)
                VALUES (:id, :org, :team, :path, :content, :chars,
                        :kind, :bot, :name, :kind, :bot, :name)
                """
            ),
            {
                "id": file_id,
                "org": organization_id,
                "team": team_id,
                "path": path,
                "content": content,
                "chars": len(content),
                "kind": editor_kind,
                "bot": editor_bot_id,
                "name": editor_name,
            },
        )

    async def save(
        self,
        file_id: uuid.UUID,
        *,
        path: str,
        content: str,
        version: int,
        editor_kind: str,
        editor_bot_id: uuid.UUID | None,
        editor_name: str,
        deleted: bool = False,
    ) -> None:
        """The file as it is after a change. `deleted` moves it to (or out of) the trash."""
        await self._s.execute(
            text(
                """
                UPDATE team_files
                   SET path = :path, content = :content, chars = :chars, version = :version,
                       updated_by_kind = :kind, updated_by_bot_id = :bot,
                       updated_by_name = :name, updated_at = now(),
                       deleted_at = CASE WHEN :deleted THEN now() ELSE NULL END
                 WHERE id = :id
                """
            ),
            {
                "id": file_id,
                "path": path,
                "content": content,
                "chars": len(content),
                "version": version,
                "kind": editor_kind,
                "bot": editor_bot_id,
                "name": editor_name,
                "deleted": deleted,
            },
        )

    async def set_locked(self, file_id: uuid.UUID, locked: bool) -> None:
        await self._s.execute(
            text("UPDATE team_files SET locked = :locked WHERE id = :id"),
            {"id": file_id, "locked": locked},
        )

    async def close_team(self, team_id: uuid.UUID) -> int:
        """A team with no bots left: its files go to the trash with it."""
        result = await self._s.execute(
            text(
                "UPDATE team_files SET deleted_at = now() WHERE team_id = :t AND deleted_at IS NULL"
            ),
            {"t": team_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- revisions ---------------------------------------------------------------

    async def revision(self, revision_id: uuid.UUID) -> FileRevisionRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_REVISION} FROM team_file_revisions WHERE id = :id"),
                {"id": revision_id},
            )
        ).one_or_none()
        return None if row is None else _revision(row)

    async def revision_at(self, file_id: uuid.UUID, version: int) -> FileRevisionRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_REVISION} FROM team_file_revisions "
                    "WHERE file_id = :f AND version = :v"
                ),
                {"f": file_id, "v": version},
            )
        ).one_or_none()
        return None if row is None else _revision(row)

    async def revisions(self, file_id: uuid.UUID, limit: int = 50) -> list[FileRevisionRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_REVISION} FROM team_file_revisions WHERE file_id = :f "
                    "ORDER BY version DESC LIMIT :limit"
                ),
                {"f": file_id, "limit": limit},
            )
        ).all()
        return [_revision(r) for r in rows]

    async def add_revision(
        self,
        revision_id: uuid.UUID,
        file_id: uuid.UUID,
        *,
        version: int,
        op: str,
        path: str,
        content: str,
        editor_kind: str,
        editor_bot_id: uuid.UUID | None,
        editor_name: str,
        run_id: uuid.UUID | None,
        note: str = "",
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO team_file_revisions (id, file_id, version, op, path, content,
                                                 editor_kind, editor_bot_id, editor_name,
                                                 run_id, note)
                VALUES (:id, :file, :version, :op, :path, :content,
                        :kind, :bot, :name, :run, :note)
                """
            ),
            {
                "id": revision_id,
                "file": file_id,
                "version": version,
                "op": op,
                "path": path,
                "content": content,
                "kind": editor_kind,
                "bot": editor_bot_id,
                "name": editor_name,
                "run": run_id,
                "note": note,
            },
        )

    async def prune_revisions(self, file_id: uuid.UUID, keep: int) -> int:
        result = await self._s.execute(
            text(
                """
                DELETE FROM team_file_revisions
                 WHERE file_id = :f AND version <= (
                       SELECT max(version) - :keep FROM team_file_revisions WHERE file_id = :f)
                """
            ),
            {"f": file_id, "keep": keep},
        )
        return int(getattr(result, "rowcount", 0) or 0)
