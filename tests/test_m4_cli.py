"""`runtime.cli spec` — the verbs, their exit codes, and what they refuse.

Exit codes are the contract a cron wrapper depends on, so they are asserted rather
than assumed: `2` means "there is something here" — a non-empty plan, or drift.

Two shapes of test, for one reason. The verbs that touch no database go through
`main()` with real `argv`, so the parser wiring is under test. The verbs that do are
called as `cmd_*(namespace, settings)` — `main()` calls `asyncio.run`, and an
`asyncio.run` inside the session's already-running loop raises before it reaches
anything worth testing. `test_the_parser_wires_every_verb` covers the half that
approach skips.
"""

from __future__ import annotations

import argparse
import uuid

import pytest

from runtime.cli import spec as spec_cli
from runtime.cli.main import build_parser, main
from runtime.cli.spec import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from runtime.domain.ids import OrganizationId
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from runtime.spec.apply import apply_org
from tests.conftest_m4 import Corpus

CORPUS = "config/org"


@pytest.fixture(autouse=True)
def _pin_logging_to_a_stream_that_stays_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep structlog off `capsys`'s streams for the whole module.

    Two structlog facts combine badly with a CLI test. `configure_logging` builds a
    `PrintLoggerFactory(file=sys.stderr)` — evaluating `sys.stderr` *at configure time*
    — and it sets `cache_logger_on_first_use=True`, so a module-level
    `log = get_logger(...)` binds that stream once and keeps it forever. `main()` calls
    `configure_logging`, so the first CLI test would pin every logger bound after it to
    that test's capsys stream, which pytest closes at teardown. Every later test then
    dies with `ValueError: I/O operation on closed file` from a log line unrelated to
    what it was testing.

    So: configure once against the real stderr, and make `main()`'s call a no-op. What
    is under test here is exit codes and stdout, and neither is affected.
    """
    import sys

    import structlog

    from runtime.observability.logging import configure_logging

    structlog.reset_defaults()
    real_stderr = sys.stderr
    monkeypatch.setattr(sys, "stderr", sys.__stderr__)
    configure_logging(level="WARNING", json=True)
    monkeypatch.setattr(sys, "stderr", real_stderr)
    monkeypatch.setattr("runtime.cli.main.configure_logging", lambda **_kwargs: None)


def ns(**fields: object) -> argparse.Namespace:
    """A namespace with the defaults every `spec` verb reads."""
    base: dict[str, object] = {
        "path": CORPUS,
        "organization": None,
        "organization_name": None,
        "no_registry": False,
        "operator": "operator",
        "rename": None,
        "allow_replace": False,
        "json": False,
        "save": False,
        "plan": None,
        "yes": True,
        "force": False,
        "source_ref": None,
        "limit": 50,
        "verbose": False,
        "kind": None,
        "name": None,
        "out": None,
    }
    base.update(fields)
    return argparse.Namespace(**base)


# --- the parser -----------------------------------------------------------------------


def test_the_parser_wires_every_verb() -> None:
    parser = build_parser()
    spec = parser._subparsers._group_actions[0].choices["spec"]  # type: ignore[union-attr]
    verbs = next(a for a in spec._actions if a.dest == "verb")
    assert set(verbs.choices) == {
        "validate",
        "show",
        "export",
        "diff",
        "plan",
        "apply",
        "drift",
        "history",
    }


def test_only_the_database_verbs_require_an_organization() -> None:
    """`validate`, `show` and `export` are pure functions of the files, which is what
    makes `validate` the thing to run in CI."""
    parser = build_parser()
    spec = parser._subparsers._group_actions[0].choices["spec"]  # type: ignore[union-attr]
    verbs = next(a for a in spec._actions if a.dest == "verb")
    needs = {name: sub.get_default("needs_org") for name, sub in verbs.choices.items()}
    assert needs["validate"] is False
    assert needs["show"] is False
    assert needs["export"] is False
    assert needs["plan"] is True
    assert needs["apply"] is True
    assert needs["diff"] is True
    assert needs["drift"] is True


def test_plan_requires_an_organization(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["spec", "plan", "--path", CORPUS]) == EXIT_ERROR
    assert "--organization is required" in capsys.readouterr().err


# --- verbs that need no database ---------------------------------------------------------


def test_validate_needs_no_organization(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["spec", "validate", "--path", CORPUS]) == EXIT_OK
    out = capsys.readouterr().out
    assert "25 document(s) ok" in out
    assert "fingerprint" in out


def test_validate_reports_a_bad_document(tmp_path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "apiVersion: agent-platform/v1\nkind: Actor\nmetadata: {name: x}\n"
        "spec: {kind: llm_agent, graph: nope}\n"
    )
    with pytest.raises(Exception, match="not version-pinned"):
        main(["spec", "validate", "--path", str(tmp_path)])


def test_show_renders_one_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["spec", "show", "--path", CORPUS, "--kind", "Actor", "--name", "research"]) == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "research@1" in out
    assert out.count("apiVersion:") == 1, "one document, not the whole corpus"
    assert "name: research" in out


def test_show_refuses_a_name_that_matches_nothing() -> None:
    assert main(["spec", "show", "--path", CORPUS, "--name", "nobody"]) == EXIT_ERROR


def test_export_writes_the_department(tmp_path) -> None:
    target = tmp_path / "out" / "org.yaml"
    assert main(["spec", "export", "--out", str(target)]) == EXIT_OK
    assert "apiVersion: agent-platform/v1" in target.read_text()
    assert main(["spec", "validate", "--path", str(target)]) == EXIT_OK


def test_export_and_the_committed_corpus_agree(tmp_path) -> None:
    """`config/org` is the export, committed and split by kind. The split is for
    humans; the content has to be identical."""
    from runtime.spec.compile import compile_org
    from runtime.spec.loader import load_path

    target = tmp_path / "org.yaml"
    main(["spec", "export", "--out", str(target)])
    assert (
        compile_org(load_path(target)).fingerprint() == compile_org(load_path(CORPUS)).fingerprint()
    )


# --- verbs that need a database -----------------------------------------------------------

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
async def test_plan_exits_two_when_there_is_something_to_do(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await spec_cli.cmd_plan(ns(organization=str(uuid.uuid4())), settings)
    assert code == EXIT_FINDINGS
    assert "+ Actor/research" in capsys.readouterr().out


@pytest.mark.integration
async def test_plan_exits_zero_when_there_is_nothing_to_do(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    organization_id = OrganizationId(uuid.uuid4())
    await apply_org(uow_factory, Corpus.department().validate(), organization_id=organization_id)
    assert await spec_cli.cmd_plan(ns(organization=str(organization_id)), settings) == EXIT_OK


@pytest.mark.integration
async def test_apply_writes(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    organization_id = uuid.uuid4()
    assert await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings) == EXIT_OK
    assert "published version 1" in capsys.readouterr().out
    async with uow_factory() as uow:
        state = await uow.spec.load_state(organization_id)
    assert len(state.actors) == 4


@pytest.mark.integration
async def test_apply_answering_no_writes_nothing(
    uow_factory: UnitOfWorkFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization_id = uuid.uuid4()
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert (
        await spec_cli.cmd_apply(ns(organization=str(organization_id), yes=False), settings)
        == EXIT_OK
    )
    async with uow_factory() as uow:
        state = await uow.spec.load_state(organization_id)
    assert not state.exists


@pytest.mark.integration
async def test_apply_reports_no_changes_the_second_time(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    organization_id = uuid.uuid4()
    await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings)
    capsys.readouterr()
    assert await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings) == EXIT_OK
    assert "no changes" in capsys.readouterr().out


@pytest.mark.integration
async def test_plan_save_then_apply_by_id(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pair §13's first risk asks for: read the diff, then apply *that* diff."""
    organization_id = uuid.uuid4()
    await spec_cli.cmd_plan(ns(organization=str(organization_id), save=True), settings)
    plan_id = capsys.readouterr().out.split("saved as ")[1].split("\n")[0].strip()

    code = await spec_cli.cmd_apply(ns(organization=str(organization_id), plan=plan_id), settings)
    assert code == EXIT_OK
    async with uow_factory() as uow:
        row = await uow.spec.get_plan(uuid.UUID(plan_id))
    assert row is not None and row["status"] == "APPLIED"


@pytest.mark.integration
async def test_applying_a_stale_plan_marks_it_stale_and_refuses(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Edge case 76 through the CLI."""
    organization_id = uuid.uuid4()
    await spec_cli.cmd_plan(ns(organization=str(organization_id), save=True), settings)
    plan_id = capsys.readouterr().out.split("saved as ")[1].split("\n")[0].strip()

    await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings)
    capsys.readouterr()

    code = await spec_cli.cmd_apply(ns(organization=str(organization_id), plan=plan_id), settings)
    assert code == EXIT_ERROR
    assert "the organization moved" in capsys.readouterr().err
    async with uow_factory() as uow:
        row = await uow.spec.get_plan(uuid.UUID(plan_id))
    assert row is not None and row["status"] == "STALE"


@pytest.mark.integration
async def test_drift_exits_zero_after_an_apply(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    organization_id = uuid.uuid4()
    await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings)
    capsys.readouterr()
    assert await spec_cli.cmd_drift(ns(organization=str(organization_id)), settings) == EXIT_OK
    assert "no drift" in capsys.readouterr().out


@pytest.mark.integration
async def test_drift_exits_two_when_the_database_moved(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    organization_id = OrganizationId(uuid.uuid4())
    await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings)
    capsys.readouterr()
    async with uow_factory.transaction() as uow:
        await uow.authority.revoke_tool(
            organization_id, subject_type="actor", subject_id="research", tool="web.fetch@1"
        )
    assert (
        await spec_cli.cmd_drift(ns(organization=str(organization_id)), settings) == EXIT_FINDINGS
    )
    assert "not granted in the database" in capsys.readouterr().out


@pytest.mark.integration
async def test_history_shows_what_an_apply_did(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    organization_id = uuid.uuid4()
    await spec_cli.cmd_apply(ns(organization=str(organization_id)), settings)
    capsys.readouterr()
    assert await spec_cli.cmd_history(ns(organization=str(organization_id)), settings) == EXIT_OK
    out = capsys.readouterr().out
    assert "create" in out
    assert "Actor/research" in out
