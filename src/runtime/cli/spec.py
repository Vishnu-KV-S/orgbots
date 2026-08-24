"""`runtime.cli spec` — the operator's side of the config plane.

    validate   parse, check, and say nothing else happened
    show       render the compiled documents, or one of them
    export     the Python-defined department, as YAML
    diff       what the files say versus what the database has
    plan       the reviewable diff, with a hash
    apply      write it, transactionally
    drift      what changed outside the files
    history    what past applies actually did

**`validate` touches no database.** That is what makes it the thing to run in CI: a
pull request that changes `config/org/` can be checked without a Postgres, and a
malformed document fails the build rather than the deploy.

**`plan` and `apply` are separate verbs on purpose**, and `apply --plan <id>` is the
pair. §13 risk 1 is a quiet bad apply and the stated guard is to treat a plan diff like
a code review — which only works if reading the diff and applying it are two acts, with
a hash tying them together (edge case 76).

**Exit codes.** `2` means "there is something here": a non-empty plan, or drift. A cron
wrapper can act on that without parsing the output, and a `0` from `spec drift` in CI is
the soft exit criterion in §12.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FINDINGS = 2

DEFAULT_SPEC_PATH = "config/org"


def _org(value: str) -> OrganizationId:
    return OrganizationId(uuid.UUID(value))


def _load(args: argparse.Namespace, *, settings: Settings) -> Any:
    """Load, validate and compile. The three steps every verb but `export` starts with."""
    from runtime.spec.compile import compile_org
    from runtime.spec.loader import load_path
    from runtime.spec.validation import Registries, default_registries, validate

    documents = load_path(args.path)
    org = compile_org(documents, organization_name=getattr(args, "organization_name", None))
    skip = bool(getattr(args, "no_registry", False))
    registries = Registries() if skip else default_registries(settings)
    validate(org, registries=registries)
    return org


def _renames(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values or []:
        old, sep, new = raw.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"--rename takes OLD=NEW, got {raw!r}")
        out[old] = new
    return out


# --- verbs ---------------------------------------------------------------------------


async def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    org = _load(args, settings=settings)
    print(f"{len(org.documents)} document(s) ok")
    print(f"organization {org.organization}")
    print(
        f"  {len(org.actors)} actor(s), {len(org.roles)} role(s), "
        f"{len(org.grants)} grant(s), {len(org.policies)} policy(ies), "
        f"{len(org.triggers)} trigger(s)"
    )
    for actor in sorted(org.actors, key=lambda a: a.name):
        print(f"  {actor.name:<20} {actor.spec.kind.value:<22} {actor.spec_hash}")
    print(f"fingerprint {org.fingerprint()}")
    return EXIT_OK


async def cmd_show(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.spec.loader import dump_documents, load_path

    documents = load_path(args.path)
    wanted = [
        d
        for d in documents
        if (not args.kind or d.kind.value.lower() == args.kind.lower())
        and (not args.name or d.name == args.name)
    ]
    if not wanted:
        print("no matching documents", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps([d.envelope() for d in wanted], indent=2, sort_keys=True))
    else:
        print(dump_documents(wanted))
    return EXIT_OK


async def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    """The Python-defined department, as documents (§10, PR-37).

    Writing to a directory rather than to stdout by default, because the corpus this
    produces is meant to be committed and then edited — which is the whole migration
    path from M1's hardcoding to M4's files.
    """
    from runtime.spec.exporter import export_department

    export = export_department(organization=args.organization_name)
    text = export.to_yaml()
    if not args.out:
        print(text)
        return EXIT_OK
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {len(export.documents)} document(s) to {target}")
    return EXIT_OK


async def _plan(args: argparse.Namespace, settings: Settings) -> Any:
    from runtime.spec.differ import build_plan

    org = _load(args, settings=settings)
    organization_id = _org(args.organization)
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        state = await uow.spec.load_state(organization_id)
    return org, build_plan(
        org,
        state,
        organization_id=organization_id,
        renames=_renames(getattr(args, "rename", None)),
        allow_replace=getattr(args, "allow_replace", False),
    )


async def cmd_diff(args: argparse.Namespace, settings: Settings) -> int:
    _, plan = await _plan(args, settings)
    print(plan.render())
    return EXIT_FINDINGS if not plan.empty else EXIT_OK


async def cmd_plan(args: argparse.Namespace, settings: Settings) -> int:
    _, plan = await _plan(args, settings)
    if args.json:
        body = {**plan.as_json(), "planHash": plan.plan_hash()}
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        print(plan.render())

    if args.save and not plan.empty:
        plan_id = uuid.uuid4()
        factory = UnitOfWorkFactory(settings)
        async with factory.transaction() as uow:
            await uow.spec.save_plan(
                plan_id,
                _org(args.organization),
                plan=plan.as_json(),
                plan_hash=plan.plan_hash(),
                created_by=args.operator,
            )
        print(f"\nsaved as {plan_id}")
        print(f"apply with:  python -m runtime.cli spec apply --plan {plan_id} ...")
        print("expires in 15 minutes (§9): a plan that is right by luck was not checked")
    return EXIT_FINDINGS if not plan.empty else EXIT_OK


async def cmd_apply(args: argparse.Namespace, settings: Settings) -> int:
    import datetime as dt

    from runtime.spec.apply import apply_org
    from runtime.spec.differ import Plan

    org, computed = await _plan(args, settings)
    organization_id = _org(args.organization)
    factory = UnitOfWorkFactory(settings)

    approved: Plan | None = None
    if args.plan:
        plan_id = uuid.UUID(args.plan)
        async with factory() as uow:
            row = await uow.spec.get_plan(plan_id)
        if row is None:
            print(f"no plan {plan_id}", file=sys.stderr)
            return EXIT_ERROR
        if row["status"] != "PENDING":
            print(f"plan {plan_id} is {row['status']}, not PENDING", file=sys.stderr)
            return EXIT_ERROR
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        # Rebuilt from the stored diff rather than trusted wholesale: what is compared
        # is the *hash*, and reconstructing it from the row is what makes the comparison
        # mean "the same diff" rather than "the same object I happen to hold".
        approved = Plan(
            organization=row["plan"]["organization"],
            organization_id=organization_id,
            changes=computed.changes,
            renames=tuple((str(r[0]), str(r[1])) for r in row["plan"].get("renames", ())),
            created_at=created,
            plan_id=plan_id,
        )
        if approved.plan_hash() != row["plan_hash"]:
            print(
                f"the organization moved since plan {plan_id} was made: "
                f"{row['plan_hash']} then, {approved.plan_hash()} now. Re-run `plan`.",
                file=sys.stderr,
            )
            async with factory.transaction() as uow:
                await uow.spec.mark_plan(plan_id, "STALE")
            return EXIT_ERROR

    if computed.empty and not args.force:
        print("no changes")
        return EXIT_OK

    if not args.yes:
        print(computed.render())
        print()
        answer = input(f"apply {len(computed.effective)} change(s)? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted")
            return EXIT_OK

    result = await apply_org(
        UnitOfWorkFactory(settings),
        org,
        organization_id=organization_id,
        plan=approved,
        renames=_renames(getattr(args, "rename", None)),
        allow_replace=args.allow_replace,
        applied_by=args.operator,
        source_ref=args.source_ref,
    )
    print(
        f"applied {result.changed} change(s): {result.created} created, "
        f"{result.updated} updated, {result.deactivated} deactivated, "
        f"{result.renamed} renamed"
    )
    for actor, version in sorted(result.versions_published.items()):
        print(f"  actor {actor:<20} published version {version}")
    if not result.versions_published:
        print("  no actor version published — every spec_hash was already current")
    for scope, before, after in result.budget_changes:
        print(f"  budget {scope:<20} {before} -> {after} cents")
    print(f"plan_hash {result.plan_hash}")
    return EXIT_OK


async def cmd_drift(args: argparse.Namespace, settings: Settings) -> int:
    from runtime.spec.drift import detect_drift

    org = _load(args, settings=settings)
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        state = await uow.spec.load_state(_org(args.organization))
    report = detect_drift(org, state)
    print(report.render())
    return EXIT_OK if report.clean else EXIT_FINDINGS


async def cmd_history(args: argparse.Namespace, settings: Settings) -> int:
    factory = UnitOfWorkFactory(settings)
    async with factory() as uow:
        events = await uow.spec.events(_org(args.organization), limit=args.limit)
    if not events:
        print("no applies recorded")
        return EXIT_OK
    for event in events:
        print(
            f"{event['created_at'].isoformat()}  {event['action']:<11} "
            f"{event['kind']}/{event['name']}  by {event['applied_by']}"
        )
        if args.verbose and event["detail"]:
            print(f"    {json.dumps(event['detail'], sort_keys=True, default=str)}")
    return EXIT_OK


# --- wiring --------------------------------------------------------------------------


def add_spec_parser(sub: Any) -> None:
    spec = sub.add_parser("spec", help="the config plane: validate, plan, apply, drift")
    verbs = spec.add_subparsers(dest="verb", required=True)

    def common(parser: Any, *, needs_org: bool = True) -> None:
        parser.add_argument("--path", default=DEFAULT_SPEC_PATH, help="a YAML file or a directory")
        parser.add_argument(
            "--organization-name",
            default=None,
            help="used when the document set has no Organization document",
        )
        parser.add_argument(
            "--no-registry",
            action="store_true",
            help="skip the tool/graph/handler existence checks (structure only)",
        )
        if needs_org:
            parser.add_argument("--operator", default="operator")

    validate = verbs.add_parser("validate", help="parse and check; touches no database")
    common(validate, needs_org=False)
    validate.set_defaults(fn=cmd_validate, needs_org=False)

    show = verbs.add_parser("show", help="render the documents")
    common(show, needs_org=False)
    show.add_argument("--kind", default=None)
    show.add_argument("--name", default=None)
    show.add_argument("--json", action="store_true")
    show.set_defaults(fn=cmd_show, needs_org=False)

    export = verbs.add_parser("export", help="the Python-defined department, as YAML")
    export.add_argument("--organization-name", default="acme")
    export.add_argument("--out", default=None, metavar="PATH")
    export.set_defaults(fn=cmd_export, needs_org=False)

    diff = verbs.add_parser("diff", help="files versus database")
    common(diff)
    diff.add_argument("--rename", action="append", metavar="OLD=NEW")
    diff.add_argument("--allow-replace", action="store_true")
    diff.set_defaults(fn=cmd_diff, needs_org=True)

    plan = verbs.add_parser("plan", help="the reviewable diff, with a hash")
    common(plan)
    plan.add_argument("--rename", action="append", metavar="OLD=NEW")
    plan.add_argument("--allow-replace", action="store_true")
    plan.add_argument("--json", action="store_true")
    plan.add_argument("--save", action="store_true", help="store it so `apply --plan` can use it")
    plan.set_defaults(fn=cmd_plan, needs_org=True)

    apply_ = verbs.add_parser("apply", help="write it, transactionally")
    common(apply_)
    apply_.add_argument("--plan", default=None, metavar="PLAN_ID")
    apply_.add_argument("--rename", action="append", metavar="OLD=NEW")
    apply_.add_argument("--allow-replace", action="store_true")
    apply_.add_argument("--yes", "-y", action="store_true", help="do not prompt")
    apply_.add_argument("--force", action="store_true", help="apply even with no changes")
    apply_.add_argument("--source-ref", default=None, help="git sha of the files")
    apply_.set_defaults(fn=cmd_apply, needs_org=True)

    drift = verbs.add_parser("drift", help="what changed outside the files")
    common(drift)
    drift.set_defaults(fn=cmd_drift, needs_org=True)

    history = verbs.add_parser("history", help="what past applies did")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--verbose", "-v", action="store_true")
    history.set_defaults(fn=cmd_history, needs_org=True)


__all__ = ["add_spec_parser"]
