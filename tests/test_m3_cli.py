"""`runtime.cli memory …`.

The CLI is where §5's grading loop and §11's evals actually get run by a person, so
what is tested is the part a person depends on: that the exit codes distinguish the
three outcomes, that `--organization` is required, and that **no command flips the
injection flag**.

That last one is the point of the file. §9: *"PR-35 is the only one that changes what
the models see."* A `memory flip` command would make that change one keystroke away from
someone who had not read the grading verdict, so it does not exist — and a test asserts
its absence, because "we decided not to add it" is not a thing a future contributor can
see.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from runtime.cli import memory as memory_cli
from runtime.cli.main import build_parser, main
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import seed_org

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _rebind_logging() -> None:
    """Point structlog at *this* test's captured streams.

    `configure_logging` builds a `PrintLoggerFactory(file=sys.stderr)` and structlog
    caches the bound logger on first use, so a logger created during an earlier test
    holds that test's `capsys` stream — which pytest closed at teardown. The first log
    line in this module then raises `ValueError: I/O operation on closed file` from
    somewhere that has nothing to do with what is under test.

    Reconfiguring per test rather than once per session, because `capsys` replaces the
    streams per test too.
    """
    import structlog

    from runtime.observability.logging import configure_logging

    structlog.reset_defaults()
    configure_logging(level="INFO", json=True)


def test_the_cli_cannot_turn_injection_on() -> None:
    """PR-35 is an environment variable and a worker restart, on purpose."""
    parser = build_parser()
    memory = next(
        action
        for action in parser._subparsers._group_actions[0].choices["memory"]._actions  # type: ignore[union-attr]
        if action.dest == "memory_command"
    )
    assert set(memory.choices) == {"status", "grade", "golden", "evals", "promotions"}
    assert "flip" not in memory.choices

    source = inspect.getsource(memory_cli)
    assert "memory_injection_enabled=True" not in source
    assert "os.environ[" not in source, (
        "the CLI is writing to the environment; injection must be an operator's "
        "deployment change, not a command"
    )


def test_memory_commands_require_an_organization(capsys: pytest.CaptureFixture[str]) -> None:
    """Every scope id is derived from the organization, so a command without one could
    not build a filter and would have to either fail or read across tenants."""
    assert main(["memory", "status"]) == 1
    assert "--organization is required" in capsys.readouterr().err


async def test_status_reports_the_backend_and_the_injection_state(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    org = uuid.uuid4()
    await seed_org(uow_factory, org)  # type: ignore[arg-type]

    import argparse

    args = argparse.Namespace(organization=org, memory_command="status")
    assert await memory_cli.cmd_memory(args, settings) == 0

    out = capsys.readouterr().out
    assert "backend" in out
    assert "shadow mode" in out, "status must say plainly that injection is off"
    assert "no memories yet" in out


async def test_evals_exit_two_only_when_a_hard_gate_fails(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The asymmetry §11 asks for: evals 1, 2, 3, 6 and 7 are numbers over a corpus that
    may be a placeholder, and failing CI on a guess teaches people to ignore CI. Only the
    three isolation gates decide the code."""
    import argparse

    org = uuid.uuid4()
    await seed_org(uow_factory, org)  # type: ignore[arg-type]
    args = argparse.Namespace(organization=org, memory_command="evals", json=False)

    assert await memory_cli.cmd_memory(args, settings) == 0
    out = capsys.readouterr().out
    assert "hard gates: 3/3 at zero" in out
    assert "not measured:" in out
    assert "§13 risk 5" in out, "an unmeasured eval must say it is not a passing eval"


async def test_the_grading_verdict_exits_two_below_the_bar(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing graded, the answer is NOT YET and the exit code says so — which is
    what makes `memory grade --verdict` usable in a checklist."""
    import argparse

    from runtime.memory.grading import GradingBar

    org = uuid.uuid4()
    await seed_org(uow_factory, org)  # type: ignore[arg-type]
    bar = GradingBar()
    args = argparse.Namespace(
        organization=org,
        memory_command="grade",
        verdict=True,
        count=10,
        seed="m3",
        reviewer="operator",
        min_helpful=bar.min_helpful_or_neutral_pct,
        max_harmful=bar.max_harmful_pct,
        min_sample=bar.min_sample,
    )
    assert await memory_cli.cmd_memory(args, settings) == memory_cli.EXIT_GATE_FAILED
    out = capsys.readouterr().out
    assert "NOT YET" in out
    assert "nothing graded yet" in out


async def test_golden_reports_the_placeholder_provenance(
    uow_factory: UnitOfWorkFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """§13 risk 5 again: a number over a placeholder corpus has to say so where somebody
    will read it, not only in a README."""
    import argparse

    args = argparse.Namespace(organization=uuid.uuid4(), memory_command="golden", build=False)
    assert await memory_cli.cmd_memory(args, settings) == 0
    out = capsys.readouterr().out
    assert "provenance      placeholder" in out
    assert "not evidence" in out
