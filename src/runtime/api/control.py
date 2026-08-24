"""The control surface — operating the organization over HTTP.

`observe.py` answers *what is happening*. This module is the other half: edit the
files that define a company, plan and apply them, stop a department, and ask an actor
to run. Until it existed, all of that lived behind a shell, which meant the canvas
could show you an organization it could not help you change.

Five rules hold it together.

1. **It never executes a run.** Same rule as `app.py`. Every verb here either writes a
   file, writes a row, or calls `RunService.start_run` — which admits and returns. A
   worker does the work. `POST .../tick` runs the scheduler and the dispatcher, and
   neither of those executes anything either; they only create runs.
2. **The organization is in the path**, matching `observe.py`'s second rule. A viewer
   that has just been shown a list of companies cannot be asked to name one in a
   header before it may read the list.
3. **Files live under `spec_roots` and nowhere else.** Every path is resolved with
   symlinks followed and refused unless it lands inside a configured root. The roots
   are a confinement boundary, not a search path, and `../` is not a special case to
   be stripped — it is handled by resolving and comparing, which is the only version
   of this check that is right for symlinks too.
4. **A save writes the operator's bytes.** Not `dump_documents`, not
   `Document.envelope()` — both emit with `sort_keys=True` and carry no comments, so
   round-tripping a file through them would silently reorder every key and delete every
   comment in `config/`. A save parses (so a broken file is refused) and then writes
   exactly what was sent.
5. **A save is not an apply.** `parse_text` runs on every write; `validate()` does not.
   Validation is a whole-corpus question, and an editor in which you cannot briefly
   have a half-finished corpus is an editor in which you cannot rename anything.

**There is no authentication.** Anything that can reach this router can stop a
department, spend money, and rewrite `config/`. `RUNTIME_SPEC_EDITABLE=false` turns off
the writes; nothing turns off the rest. It belongs on a local devstack, behind
something, or not exposed at all.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from runtime.api.errors import http_errors
from runtime.domain.enums import KillMode, KillScope
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import StartRunRequest
from runtime.observability.logging import get_logger
from runtime.org.killswitch import KILL_CACHE_TTL_SECONDS, KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.dispatcher import Dispatcher
from runtime.runtime.run_service import RunService
from runtime.runtime.scheduler import Scheduler
from runtime.settings import Settings
from runtime.spec.documents import SPEC_BY_KIND, Kind
from runtime.spec.loader import YAML_SUFFIXES, load_path, parse_text

log = get_logger("api.control")

router = APIRouter(prefix="/v1/control", tags=["control"])


# --- process wiring ------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _uow(request: Request) -> UnitOfWorkFactory:
    factory: UnitOfWorkFactory = request.app.state.uow
    return factory


def _service(request: Request) -> RunService:
    service: RunService = request.app.state.service
    return service


# --- path confinement ----------------------------------------------------------------


def spec_roots(settings: Settings) -> tuple[Path, ...]:
    """The configured roots, resolved. The one place `spec_roots` is split."""
    return tuple(
        Path(part.strip()).resolve() for part in settings.spec_roots.split(",") if part.strip()
    )


def _confine(settings: Settings, raw: str, *, must_be_yaml: bool = True) -> Path:
    """Resolve a client-supplied path, or refuse it with 403.

    Resolution happens **before** the comparison, which is what makes this correct for
    the two attacks that are not the same attack: `../../etc/passwd` normalises out of
    the root, and a symlink inside the root that points out of it resolves out of it.
    Stripping `..` from the string would stop the first and none of the second.

    403 rather than 404, deliberately: 404 would let a caller map the filesystem by
    watching which refusals were which.
    """
    roots = spec_roots(settings)
    if not roots:
        raise HTTPException(status_code=403, detail="no spec roots are configured")
    if not raw or raw in {".", "/"}:
        raise HTTPException(status_code=403, detail="refused: empty path")

    resolved = Path(raw).resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail=(
                f"refused: {raw} is outside the configured spec roots "
                f"({', '.join(str(r) for r in roots)})"
            ),
        )
    if must_be_yaml and resolved.suffix not in YAML_SUFFIXES:
        raise HTTPException(
            status_code=403,
            detail=f"refused: {resolved.suffix or 'no suffix'} is not one of {YAML_SUFFIXES}",
        )
    return resolved


def _require_editable(settings: Settings) -> None:
    if not settings.spec_editable:
        raise HTTPException(
            status_code=403,
            detail=(
                "RUNTIME_SPEC_EDITABLE is false: this runtime serves the spec files "
                "read-only. Reads and plans still work."
            ),
        )


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _relative(path: Path) -> str:
    """A path as the client should send it back: relative to the process's cwd.

    Falls back to the absolute form for a root configured outside the tree, which is
    unusual but not an error.
    """
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# --- request bodies ------------------------------------------------------------------


class FileWrite(BaseModel):
    path: str
    content: str
    base_sha256: str | None = None
    """The digest of the bytes the editor started from.

    `None` means "I am creating this file" and is refused if it exists. A mismatch is
    409: somebody — another tab, the CLI, `git checkout` — changed the file while it
    was open, and overwriting them silently is the one failure an editor must not have.
    """


class SpecTarget(BaseModel):
    path: str
    organization_name: str | None = None
    """Used when the document set has no `Organization` document, exactly as
    `spec validate --organization-name` is."""


class PlanBody(SpecTarget):
    renames: dict[str, str] = Field(default_factory=dict)
    """`{old: new}`. Renaming an actor is a delete plus a create — the new one has no
    version history, no memory and no task history — so `build_plan` refuses it unless
    somebody says they meant it. Without this field on the wire, renaming an actor in
    the editor would dead-end on a `RenameRefused` with no way to answer it."""
    allow_replace: bool = False
    """The other answer to the same refusal: *the actor I removed and the actor I added
    are genuinely different actors*."""
    save: bool = True
    """Store the plan so `apply` can name it. The reviewable-diff flow needs this;
    a caller that only wants to look can turn it off."""


class ApplyBody(PlanBody):
    plan_id: UUID | None = None
    """The plan a human read. Omitting it applies the diff computed right now, which is
    right for a script and wrong for anything a person reviewed."""


class StopBody(BaseModel):
    mode: Literal["drain", "halt"] = "drain"
    reason: str = Field(default="stopped from the control surface", max_length=500)


class StartBody(BaseModel):
    reason: str = Field(default="started from the control surface", max_length=500)


class RunBody(BaseModel):
    mode: str = Field(default="", max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    """`None` mints a fresh one, so two clicks are two runs. Supplying one makes the
    second click a no-op, which is what a retry wants and what a nervous operator
    pressing the button twice does not."""


# --- the files -----------------------------------------------------------------------


def _kind_counts(files: Iterable[Path]) -> tuple[dict[str, int], str | None, str | None]:
    """Count documents by kind across a folder, and find the organization it declares.

    Returns `(counts, organization_name, error)`. A folder with one broken file still
    lists — the error is reported beside the counts rather than instead of them, because
    "this folder is unopenable" is exactly when somebody needs to open it.
    """
    counts: dict[str, int] = {}
    organization: str | None = None
    for file in files:
        try:
            documents = parse_text(file.read_text(encoding="utf-8"), source=str(file))
        except Exception as exc:  # reported beside the counts, not raised
            return counts, organization, str(exc)
        for document in documents:
            counts[document.kind.value] = counts.get(document.kind.value, 0) + 1
            if document.kind is Kind.ORGANIZATION:
                organization = document.metadata.name
    return counts, organization, None


@router.get("/specs")
async def list_specs(request: Request) -> dict[str, Any]:
    """The spec folders under the configured roots, with what each one contains.

    **The root itself is not a folder here.** `config/agents.example.yaml` sits loose
    at the root of `config/`, and treating the root as a spec folder would try to
    compile it together with every department below it. A spec folder is a
    subdirectory; the loose files are reported separately so they are not merely
    invisible.
    """
    settings = _settings(request)
    async with _uow(request)() as uow:
        existing = {
            r.name: str(r.id)
            for r in (await uow.session.execute(text("SELECT id, name FROM organizations"))).all()
        }

    roots: list[dict[str, Any]] = []
    for root in spec_roots(settings):
        if not root.is_dir():
            roots.append({"path": _relative(root), "exists": False, "folders": [], "loose": []})
            continue
        folders: list[dict[str, Any]] = []
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            yaml_files = sorted(
                p for p in entry.rglob("*") if p.is_file() and p.suffix in YAML_SUFFIXES
            )
            # A folder can hold things the loader ignores — `config/org/README.md` is
            # one — and listing them is how somebody finds out why their edit did
            # nothing.
            other = sorted(
                p for p in entry.rglob("*") if p.is_file() and p.suffix not in YAML_SUFFIXES
            )
            counts, organization, error = _kind_counts(yaml_files)
            folders.append(
                {
                    "path": _relative(entry),
                    "name": entry.name,
                    "files": [
                        {"path": _relative(f), "name": f.name, "size": f.stat().st_size}
                        for f in yaml_files
                    ],
                    "other": [_relative(f) for f in other],
                    "kinds": counts,
                    "organization": organization,
                    "organization_id": existing.get(organization or ""),
                    "error": error,
                }
            )
        roots.append(
            {
                "path": _relative(root),
                "exists": True,
                "folders": folders,
                "loose": [
                    _relative(p)
                    for p in sorted(root.iterdir())
                    if p.is_file() and p.suffix in YAML_SUFFIXES
                ],
            }
        )
    return {"roots": roots, "editable": settings.spec_editable}


@router.get("/specs/schema")
async def spec_schema() -> dict[str, Any]:
    """The JSON Schema for every document kind, straight off the Pydantic models.

    Eleven kinds, from `SPEC_BY_KIND` rather than a hand-kept list, so a kind added to
    the config plane appears here without anybody remembering to add it.
    """
    return {
        "apiVersion": "agent-platform/v1",
        "kinds": {kind.value: model.model_json_schema() for kind, model in SPEC_BY_KIND.items()},
    }


@router.get("/specs/file")
async def read_file(request: Request, path: str = Query(...)) -> dict[str, Any]:
    resolved = _confine(_settings(request), path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no file {path}")
    content = resolved.read_text(encoding="utf-8")
    return {
        "path": _relative(resolved),
        "content": content,
        "sha256": _digest(content),
        "size": len(content.encode("utf-8")),
        "modified_at": dt.datetime.fromtimestamp(resolved.stat().st_mtime, tz=dt.UTC).isoformat(),
    }


def _write_atomically(target: Path, content: str) -> None:
    """Write through a temporary file in the same directory, then `os.replace`.

    `os.replace` is atomic within a filesystem, so a crashed or killed process leaves
    either the old file or the new one — never the truncated middle of a document that
    the loader would then refuse and an operator would then have to reconstruct.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parse_or_422(content: str, where: str) -> int:
    """Refuse to save something the loader cannot read. Returns the document count.

    This is the *only* check a save makes. `validate()` is not called: it is a
    whole-corpus question, and a corpus you cannot briefly leave incoherent is a corpus
    in which you cannot rename a department.
    """
    with http_errors():
        return len(parse_text(content, source=where))


@router.put("/specs/file")
async def write_file(body: FileWrite, request: Request) -> dict[str, Any]:
    """Save a file, guarded by the digest the editor started from.

    The bytes are written as sent. Re-emitting the parsed value — which is what
    `dump_documents` does, with `sort_keys=True` — would reorder every key and drop
    every comment in `config/`, turning a one-line edit into an unreviewable diff.
    """
    settings = _settings(request)
    _require_editable(settings)
    resolved = _confine(settings, body.path)

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no file {body.path}; POST to create it")
    current = resolved.read_text(encoding="utf-8")
    digest = _digest(current)
    if body.base_sha256 is None:
        raise HTTPException(
            status_code=409,
            detail="base_sha256 is required to overwrite an existing file",
        )
    if body.base_sha256 != digest:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{body.path} changed since it was opened ({body.base_sha256[:12]} then, "
                f"{digest[:12]} now). Re-read it and re-apply the edit."
            ),
        )

    documents = _parse_or_422(body.content, _relative(resolved))
    _write_atomically(resolved, body.content)
    log.info("control.file_written", path=_relative(resolved), documents=documents)
    return {"path": _relative(resolved), "sha256": _digest(body.content), "documents": documents}


@router.post("/specs/file", status_code=status.HTTP_201_CREATED)
async def create_file(body: FileWrite, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    _require_editable(settings)
    resolved = _confine(settings, body.path)
    if resolved.exists():
        raise HTTPException(status_code=409, detail=f"{body.path} already exists; PUT to replace")

    documents = _parse_or_422(body.content, _relative(resolved))
    _write_atomically(resolved, body.content)
    log.info("control.file_created", path=_relative(resolved), documents=documents)
    return {"path": _relative(resolved), "sha256": _digest(body.content), "documents": documents}


@router.delete("/specs/file")
async def delete_file(
    request: Request,
    path: str = Query(...),
    base_sha256: str | None = Query(default=None),
) -> dict[str, Any]:
    """Delete a spec file. `base_sha256` is required, for the same reason PUT needs it.

    Deleting a file somebody else just changed is the same mistake as overwriting it,
    and it is the less recoverable one — which is the whole argument for this
    repository being under git.
    """
    settings = _settings(request)
    _require_editable(settings)
    resolved = _confine(settings, path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no file {path}")
    digest = _digest(resolved.read_text(encoding="utf-8"))
    if base_sha256 != digest:
        raise HTTPException(
            status_code=409,
            detail=f"{path} changed since it was read; re-read it before deleting",
        )
    resolved.unlink()
    log.warning("control.file_deleted", path=_relative(resolved))
    return {"path": _relative(resolved), "deleted": True}


# --- validate, plan, apply -----------------------------------------------------------


def _compile(settings: Settings, target: SpecTarget) -> Any:
    """Load, validate and compile — the three steps every verb but the reader starts
    with, and the same three `runtime.cli.spec._load` performs."""
    from runtime.spec.compile import compile_org
    from runtime.spec.validation import default_registries, validate

    resolved = _confine(settings, target.path, must_be_yaml=False)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no spec folder {target.path}")
    with http_errors():
        documents = load_path(resolved)
        org = compile_org(documents, organization_name=target.organization_name)
        validate(org, registries=default_registries(settings))
    return org


@router.post("/specs/validate")
async def validate_spec(body: SpecTarget, request: Request) -> dict[str, Any]:
    """Parse, check and say nothing else happened. Touches no database.

    Same verb as `spec validate`, and the same argument for it: this is the check that
    can run before anything is written, so a malformed corpus is a refusal rather than
    a half-applied organization.
    """
    org = _compile(_settings(request), body)
    return {
        "ok": True,
        "organization": org.organization,
        "fingerprint": org.fingerprint(),
        "documents": len(org.documents),
        "actors": [
            {"name": a.name, "kind": a.spec.kind.value, "spec_hash": a.spec_hash}
            for a in sorted(org.actors, key=lambda a: a.name)
        ],
        "counts": {
            "roles": len(org.roles),
            "grants": len(org.grants),
            "policies": len(org.policies),
            "triggers": len(org.triggers),
        },
    }


def _plan_view(plan: Any, *, plan_id: uuid.UUID | None = None) -> dict[str, Any]:
    from runtime.spec.differ import STALE_AFTER_SECONDS

    created = plan.created_at
    return {
        "plan_id": str(plan_id) if plan_id else None,
        "plan_hash": plan.plan_hash(),
        "empty": plan.empty,
        "render": plan.render(),
        "changes": plan.as_json()["changes"],
        "renames": [list(r) for r in plan.renames],
        "warnings": list(plan.warnings),
        "created_at": created.isoformat() if created else None,
        # The window is on the wire so the UI can show a countdown rather than
        # discovering the expiry as a 409 after somebody has read the diff. Fifteen
        # minutes is long enough to read a diff and short enough that the diff is still
        # the diff.
        "expires_in_seconds": STALE_AFTER_SECONDS,
        "expires_at": (
            (created + dt.timedelta(seconds=STALE_AFTER_SECONDS)).isoformat() if created else None
        ),
    }


async def _build(
    request: Request, body: PlanBody, organization_id: OrganizationId
) -> tuple[Any, Any]:
    from runtime.spec.differ import build_plan

    settings = _settings(request)
    org = _compile(settings, body)
    async with _uow(request)() as uow:
        state = await uow.spec.load_state(organization_id)
    with http_errors():
        plan = build_plan(
            org,
            state,
            organization_id=organization_id,
            renames=body.renames or None,
            allow_replace=body.allow_replace,
        )
    return org, plan


@router.post("/organizations/{org_id}/plan")
async def plan_spec(org_id: UUID, body: PlanBody, request: Request) -> dict[str, Any]:
    """The reviewable diff, with a hash and an expiry.

    `plan` and `apply` are two verbs on purpose: §13's first risk is a quiet bad apply,
    and the stated guard is to read the diff like a code review — which only works if
    reading it and applying it are separate acts with a hash tying them together.
    """
    organization_id = OrganizationId(org_id)
    _, plan = await _build(request, body, organization_id)

    plan_id: uuid.UUID | None = None
    if body.save and not plan.empty:
        plan_id = uuid.uuid4()
        async with _uow(request).transaction() as uow:
            await uow.spec.save_plan(
                plan_id,
                organization_id,
                plan=plan.as_json(),
                plan_hash=plan.plan_hash(),
                created_by=_settings(request).spec_operator,
            )
        # `save_plan` stamps `created_at` in the database; the object in hand has none,
        # so the window is reported from the row rather than guessed.
        async with _uow(request)() as uow:
            row = await uow.spec.get_plan(plan_id)
        if row is not None:
            created = row["created_at"]
            plan = _with_created_at(plan, created)

    return _plan_view(plan, plan_id=plan_id)


def _with_created_at(plan: Any, created: dt.datetime | None) -> Any:
    import dataclasses

    if created is None:
        return plan
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return dataclasses.replace(plan, created_at=created)


@router.post("/organizations/{org_id}/apply")
async def apply_spec(org_id: UUID, body: ApplyBody, request: Request) -> dict[str, Any]:
    """Write it, transactionally.

    Three refusals, each a 409 and each a different thing having gone wrong:

    - the plan is not `PENDING` — somebody already applied it, or it went stale. This
      is checked here rather than left to the applier so that clicking *apply* twice is
      a clean conflict instead of a confusing second success.
    - the recomputed `plan_hash` moved — the organization changed under the diff a
      human read (`PlanStale`).
    - another apply holds the organization's advisory lock (`ApplyConflict`).
    """
    from runtime.spec.apply import apply_org
    from runtime.spec.differ import Plan

    settings = _settings(request)
    _require_editable(settings)
    organization_id = OrganizationId(org_id)
    org, computed = await _build(request, body, organization_id)

    approved: Plan | None = None
    if body.plan_id is not None:
        async with _uow(request)() as uow:
            row = await uow.spec.get_plan(body.plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no plan {body.plan_id}")
        if row["status"] != "PENDING":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"plan {body.plan_id} is {row['status']}, not PENDING. A plan is "
                    "applied once; re-plan to get a diff that describes what is left."
                ),
            )
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        # Rebuilt from the stored diff rather than trusted wholesale, exactly as the
        # CLI does: what is compared is the *hash*, and reconstructing it from the row
        # is what makes the comparison mean "the same diff".
        approved = Plan(
            organization=row["plan"]["organization"],
            organization_id=organization_id,
            changes=computed.changes,
            renames=tuple((str(r[0]), str(r[1])) for r in row["plan"].get("renames", ())),
            created_at=created,
            plan_id=body.plan_id,
        )
        # **This comparison is the staleness check, and it has to be here.** The one
        # inside `apply_org` compares the plan it was handed against a plan it just
        # recomputed — and since the object above is built from `computed.changes`, the
        # two are equal by construction and that check can never fail from this caller.
        # What detects a moved organization is comparing the *rebuilt* hash against the
        # one stored when the plan was made, which is exactly what `cli/spec.py` does.
        if approved.plan_hash() != row["plan_hash"]:
            async with _uow(request).transaction() as uow:
                await uow.spec.mark_plan(body.plan_id, "STALE")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the organization moved since this plan was made: plan_hash "
                    f"{row['plan_hash']} then, {approved.plan_hash()} now. Somebody "
                    "else applied something. Re-plan and read the new diff — applying "
                    "this one would apply a change nobody reviewed."
                ),
            )
        if approved.is_stale():
            async with _uow(request).transaction() as uow:
                await uow.spec.mark_plan(body.plan_id, "STALE")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"plan {body.plan_id} was made at {created.isoformat()} and is older "
                    "than the 15-minute window. Re-plan: a plan that is right by luck is "
                    "not a plan that was checked."
                ),
            )

    if computed.empty and approved is None:
        return {"applied": False, "detail": "no changes", "plan_hash": computed.plan_hash()}

    with http_errors():
        result = await apply_org(
            _uow(request),
            org,
            organization_id=organization_id,
            plan=approved,
            renames=body.renames or None,
            allow_replace=body.allow_replace,
            applied_by=settings.spec_operator,
            source_ref=_source_ref(),
        )

    log.info(
        "control.applied",
        organization_id=str(organization_id),
        plan_hash=result.plan_hash,
        changed=result.changed,
    )
    return {
        "applied": True,
        "changed": result.changed,
        "created": result.created,
        "updated": result.updated,
        "deactivated": result.deactivated,
        "renamed": result.renamed,
        "plan_hash": result.plan_hash,
        "versions_published": dict(sorted(result.versions_published.items())),
        "budget_changes": [
            {"scope": scope, "before": before, "after": after}
            for scope, before, after in result.budget_changes
        ],
    }


def _source_ref() -> str | None:
    """The git sha of the working tree, if this checkout is a repository.

    `apply_events.source_ref` has always wanted "the git sha of the files" and, applied
    from anywhere but a CI job that passed one, has always recorded nothing. Reading it
    here is cheap and makes the apply history answer *which files*, not merely *when*.
    A dirty tree is marked, because a sha that does not describe what was applied is
    worse than no sha.
    """
    try:
        import subprocess  # imported on this path only

        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if head.returncode != 0:
            return None
        sha = head.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return f"{sha}-dirty" if dirty.stdout.strip() else sha
    except Exception:  # provenance is a nicety; it must never fail an apply
        return None


@router.post("/organizations/{org_id}/drift")
async def drift(org_id: UUID, body: SpecTarget, request: Request) -> dict[str, Any]:
    """What changed outside the files. Reports; never heals.

    A POST rather than a GET because it needs a folder to compare against and a body is
    where that belongs — the same reason `plan` is a POST.
    """
    from runtime.spec.drift import detect_drift

    org = _compile(_settings(request), body)
    async with _uow(request)() as uow:
        state = await uow.spec.load_state(OrganizationId(org_id))
    report = detect_drift(org, state)
    return {
        "clean": report.clean,
        "render": report.render(),
        "findings": [
            {"source": f.source, "subject": f.subject, "detail": f.detail} for f in report.findings
        ],
    }


@router.get("/organizations/{org_id}/history")
async def history(
    org_id: UUID, request: Request, limit: int = Query(default=50, ge=1, le=500)
) -> dict[str, Any]:
    """What past applies actually did.

    The whole mitigation for "an apply cannot be undone" is that it can be *read*, and
    a record only the CLI could reach was a mitigation only somebody with a shell had.
    """
    async with _uow(request)() as uow:
        events = await uow.spec.events(OrganizationId(org_id), limit=limit)
    return {
        "events": [
            {
                "kind": e["kind"],
                "name": e["name"],
                "action": e["action"],
                "detail": e["detail"],
                "applied_by": e["applied_by"],
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in events
        ]
    }


# --- departments ---------------------------------------------------------------------

DEPARTMENT_ACTORS_SQL = """
SELECT a.name,
       (SELECT count(*) FROM runs r
         WHERE r.actor_id = a.id AND r.status IN ('QUEUED','RUNNING')) AS live_runs
  FROM actors a
 WHERE a.organization_id = :org AND a.active AND a.department = :dept
 ORDER BY a.name
"""


async def _department_view(uow: Any, organization_id: OrganizationId, row: Any) -> dict[str, Any]:
    """One department, with its state *derived* rather than stored.

    Nothing writes a department's state, and nothing should: a stored state is a second
    answer to a question the switches and the triggers already answer, and the two would
    disagree the first time somebody used the CLI. The derivation, in precedence order:

        stopped   an org- or department-scoped kill switch covers it
        running   a trigger is active, or one of its members has a live run
        paused    neither — it exists, it is not stopped, and nothing will wake it
    """
    members = (
        await uow.session.execute(
            text(DEPARTMENT_ACTORS_SQL), {"org": organization_id, "dept": row.name}
        )
    ).all()
    names = {m.name for m in members}
    live_runs = sum(int(m.live_runs) for m in members)

    switches = await uow.killswitch.active(organization_id)
    covering = [
        s
        for s in switches
        if s.scope_type is KillScope.ORG
        or (s.scope_type is KillScope.DEPARTMENT and s.scope_id == row.name)
    ]
    triggers = [t for t in await uow.triggers.for_org(organization_id) if t.actor_name in names]
    active_triggers = [t for t in triggers if t.active]

    if covering:
        state = "stopped"
    elif active_triggers or live_runs:
        state = "running"
    else:
        state = "paused"

    return {
        "name": row.name,
        "description": row.description,
        "head": row.head_actor_name,
        "parent": None,
        "active": row.active,
        "state": state,
        "members": sorted(names),
        "live_runs": live_runs,
        "triggers": [
            {
                "key": t.key,
                "actor": t.actor_name,
                "cron": t.cron,
                "timezone": t.timezone,
                "active": t.active,
                "last_evaluated_at": (
                    t.last_evaluated_at.isoformat() if t.last_evaluated_at else None
                ),
            }
            for t in triggers
        ],
        "kill_switch": (
            {
                "scope_type": covering[0].scope_type.value,
                "scope_id": covering[0].scope_id,
                "mode": covering[0].mode.value,
                "reason": covering[0].reason,
                "engaged_by": covering[0].engaged_by,
                "engaged_at": covering[0].engaged_at.isoformat(),
            }
            if covering
            else None
        ),
    }


@router.get("/organizations/{org_id}/departments")
async def list_departments(org_id: UUID, request: Request) -> dict[str, Any]:
    organization_id = OrganizationId(org_id)
    async with _uow(request)() as uow:
        rows = await uow.departments.all_for(organization_id)
        departments = [await _department_view(uow, organization_id, r) for r in rows]
    return {"departments": departments, "propagation_seconds": KILL_CACHE_TTL_SECONDS}


async def _department_or_404(request: Request, organization_id: OrganizationId, name: str) -> Any:
    async with _uow(request)() as uow:
        row = await uow.departments.get(organization_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no department {name}")
    return row


async def _member_trigger_keys(
    request: Request, organization_id: OrganizationId, department: str
) -> list[str]:
    async with _uow(request)() as uow:
        members = {
            m.name
            for m in (
                await uow.session.execute(
                    text(DEPARTMENT_ACTORS_SQL),
                    {"org": organization_id, "dept": department},
                )
            ).all()
        }
        return [
            t.key for t in await uow.triggers.for_org(organization_id) if t.actor_name in members
        ]


@router.post("/organizations/{org_id}/departments/{name}/stop")
async def stop_department(
    org_id: UUID, name: str, body: StopBody, request: Request
) -> dict[str, Any]:
    """Stop a department: engage the switch, then pause its triggers. In that order.

    **Two mechanisms, because neither alone is a stop.** Pausing the triggers stops new
    work being scheduled and does nothing about work already in flight — and the next
    `spec apply` un-pauses them, because `TriggerRepository.upsert` forces
    `active = true` and the files are the intent. The kill switch is the authoritative
    half: it refuses new runs and new tool calls for every member, and no apply touches
    it. So the switch goes first, and if it fails nothing else happens.

    A second live switch for the same scope is refused rather than silently replacing
    the first — two switches in different modes is an ambiguity nobody resolves
    correctly at 3am — so `engage()` returning False is a 409, not a retry.

    It takes up to `KILL_CACHE_TTL_SECONDS` for a worker process to see this. The
    kill-switch cache is per-process and `invalidate()` clears only the caller's.
    """
    settings = _settings(request)
    organization_id = OrganizationId(org_id)
    await _department_or_404(request, organization_id, name)

    switches = KillSwitchService(_uow(request))
    engaged = await switches.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=name,
        mode=KillMode(body.mode),
        reason=body.reason,
        engaged_by=settings.spec_operator,
    )
    if not engaged:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a kill switch is already live for department {name}. Disengage it "
                "first — two live switches in different modes is an ambiguity nobody "
                "resolves correctly under pressure."
            ),
        )

    keys = await _member_trigger_keys(request, organization_id, name)
    async with _uow(request).transaction() as uow:
        paused = await uow.triggers.set_active(organization_id, keys, False)

    log.warning(
        "control.department_stopped",
        organization_id=str(organization_id),
        department=name,
        mode=body.mode,
        triggers_paused=paused,
    )
    return {
        "department": name,
        "stopped": True,
        "mode": body.mode,
        "triggers_paused": paused,
        "propagation_seconds": KILL_CACHE_TTL_SECONDS,
        "detail": (
            f"a worker may take up to {KILL_CACHE_TTL_SECONDS:.0f}s to see this: the "
            "kill-switch cache is per-process."
        ),
    }


@router.post("/organizations/{org_id}/departments/{name}/start")
async def start_department(
    org_id: UUID, name: str, body: StartBody, request: Request
) -> dict[str, Any]:
    """Undo a stop: disengage the switch, resume the triggers.

    Not a 409 when nothing was stopped. *Start* is the idempotent direction — a
    department that is already running is the state the caller asked for — and refusing
    it would make the button lie about what it did.
    """
    settings = _settings(request)
    organization_id = OrganizationId(org_id)
    await _department_or_404(request, organization_id, name)

    switches = KillSwitchService(_uow(request))
    disengaged = await switches.disengage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=name,
        disengaged_by=settings.spec_operator,
    )
    keys = await _member_trigger_keys(request, organization_id, name)
    async with _uow(request).transaction() as uow:
        resumed = await uow.triggers.set_active(organization_id, keys, True)

    log.warning(
        "control.department_started",
        organization_id=str(organization_id),
        department=name,
        disengaged=disengaged,
        triggers_resumed=resumed,
        reason=body.reason,
    )
    return {
        "department": name,
        "kill_switch_disengaged": disengaged,
        "triggers_resumed": resumed,
        "propagation_seconds": KILL_CACHE_TTL_SECONDS,
    }


# --- the crank -----------------------------------------------------------------------


@router.post("/organizations/{org_id}/tick")
async def tick(org_id: UUID, request: Request) -> dict[str, Any]:
    """One turn of the crank, by hand: cron fires become runs, inbox messages become
    runs. Nothing here executes a run — both halves only call `RunService.start_run`.

    It is the same work the worker's conductor does on a timer, offered as a button for
    the case where somebody wants to see it happen now. **It is not how to drive the
    two-week clean run**: `docs/MEASUREMENT_PROTOCOL.md` §4 counts a person doing this
    as an intervention, and the conductor exists precisely so that nobody has to.

    The scheduler half is scoped to this organization. The dispatcher half is not, and
    cannot be: `inbox.pending` polls the whole table. It is the same drain the
    conductor performs every second anyway.
    """
    settings = _settings(request)
    factory = _uow(request)
    service = _service(request)
    organization_id = OrganizationId(org_id)

    scheduler = Scheduler(factory, service, settings=settings)
    dispatcher = Dispatcher(factory, service, settings=settings, actors=None)

    with http_errors():
        fired = await scheduler.tick(organization_id=organization_id)
        dispatched = await dispatcher.drain()

    return {
        "fired": [
            {
                "trigger": f.trigger,
                "scheduled_for": f.scheduled_for.isoformat(),
                "run_id": f.run_id,
                "skipped": f.skipped,
            }
            for f in fired
        ],
        "dispatched": dispatched,
        "detail": "the runs are queued; a worker executes them",
    }


@router.post("/organizations/{org_id}/actors/{name}/run", status_code=status.HTTP_202_ACCEPTED)
async def start_actor_run(
    org_id: UUID, name: str, body: RunBody, request: Request
) -> dict[str, Any]:
    """Ask an actor to run. 202, not 201: accepted, not finished.

    It goes through `RunService.start_run` like every other run — same admission, same
    authority, same budget, same kill switch. That is the whole reason this endpoint
    exists here rather than as a second door: a control surface that could create a run
    around those checks would be the one hole in them.
    """
    service = _service(request)
    payload = dict(body.input)
    if body.mode:
        payload["mode"] = body.mode

    with http_errors():
        result = await service.start_run(
            StartRunRequest(
                organization_id=OrganizationId(org_id),
                actor_name=name,
                input=payload,
                idempotency_key=body.idempotency_key or f"control:{uuid.uuid4()}",
            )
        )
    return {
        "run_id": str(result.run_id),
        "status": result.status.value,
        "spec_hash": result.spec_hash,
        "created": result.created,
    }
