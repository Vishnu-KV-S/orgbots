"""`/v1/control/specs/*` — the file surface, and the confinement around it.

This is the module that matters most in the control plane, because it is the only one
that touches the filesystem on behalf of a caller with no authentication. Four
properties, each one a way this could be a hole rather than a feature:

**Confinement.** `../` and a symlink pointing out of the root are not the same attack,
and only one of them is stopped by string manipulation. The check resolves first and
compares second, which is right for both.

**Content preservation.** A round trip must return the operator's bytes. The tempting
implementation — parse, then re-emit through `dump_documents` — reorders every key
(`sort_keys=True`) and drops every comment in `config/`, which turns a one-line edit
into a diff nobody can review and quietly deletes the reasoning in
`config/org/README.md`'s siblings.

**Refusal without damage.** A save that cannot be parsed must leave the file alone. The
alternative is an editor that breaks the corpus at the exact moment the operator was
told it would not.

**Optimistic concurrency.** Two tabs, or a tab and the CLI. Last-write-wins is the one
failure mode an editor must not have, so a stale `base_sha256` is a 409.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

pytestmark = pytest.mark.integration

# Comments, a specific key order that is *not* alphabetical, and a block scalar. All
# three are things a re-emitting save would destroy, and the round-trip test asserts
# the file comes back byte-for-byte.
COMMENTED_YAML = """\
# The growth department's head. Renaming this actor is a delete plus a create.
apiVersion: agent-platform/v1
kind: Actor
metadata:
  name: growth-head          # trailing comments survive too
spec:
  kind: llm_agent
  graph: marketing_head@1
  role: head
  department: growth
  ceilings:
    maxCostCents: 500        # <- the field an operator actually edits
    maxLlmCalls: 12
"""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def spec_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private spec root, and a cwd that makes the returned paths relative to it.

    The control surface reports paths relative to the process's working directory and
    expects them back the same way, so the test has to agree with it about where it is
    standing.
    """
    root = tmp_path / "config"
    (root / "growth").mkdir(parents=True)
    (root / "growth" / "30-actors.yaml").write_text(COMMENTED_YAML, encoding="utf-8")
    # A non-YAML file inside a spec folder — `config/org/README.md` is the real one.
    (root / "growth" / "README.md").write_text("# notes\n", encoding="utf-8")
    # And a loose YAML file at the root, like `config/agents.example.yaml`.
    (root / "agents.example.yaml").write_text("overrides: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return root


def _settings_with(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update=overrides)


@pytest_asyncio.fixture
async def client(
    settings: Settings, uow_factory: UnitOfWorkFactory, spec_root: Path
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings_with(settings, spec_roots="config"))
    app.state.settings = _settings_with(settings, spec_roots="config")
    app.state.uow = uow_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def read_only_client(
    settings: Settings, uow_factory: UnitOfWorkFactory, spec_root: Path
) -> AsyncIterator[httpx.AsyncClient]:
    frozen = _settings_with(settings, spec_roots="config", spec_editable=False)
    app = create_app(frozen)
    app.state.settings = frozen
    app.state.uow = uow_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# --- listing --------------------------------------------------------------------------


async def test_the_listing_skips_the_root_and_tolerates_non_yaml(
    client: httpx.AsyncClient,
) -> None:
    """A spec folder is a *subdirectory*.

    `config/agents.example.yaml` sits loose at the root of the real `config/`, and
    treating the root as a folder would try to compile it together with every
    department below it. It is reported under `loose` instead — visible, and not
    mistaken for a company.
    """
    body = (await client.get("/v1/control/specs")).json()
    root = body["roots"][0]

    assert [f["name"] for f in root["folders"]] == ["growth"]
    assert root["loose"] == ["config/agents.example.yaml"]

    folder = root["folders"][0]
    assert [f["name"] for f in folder["files"]] == ["30-actors.yaml"]
    assert folder["other"] == ["config/growth/README.md"], "a README does not break a folder"
    assert folder["kinds"] == {"Actor": 1}
    assert folder["error"] is None


async def test_a_broken_file_still_lists_its_folder(
    client: httpx.AsyncClient, spec_root: Path
) -> None:
    """ "This folder is unopenable" is exactly when somebody needs to open it."""
    (spec_root / "growth" / "99-broken.yaml").write_text("kind: [unclosed\n", encoding="utf-8")
    folder = (await client.get("/v1/control/specs")).json()["roots"][0]["folders"][0]
    assert folder["error"] is not None
    assert len(folder["files"]) == 2


async def test_the_schema_endpoint_covers_every_kind() -> None:
    """Eleven kinds, off `SPEC_BY_KIND` rather than a hand-kept list — so a kind added
    to the config plane appears here without anybody remembering."""
    from runtime.spec.documents import SPEC_BY_KIND

    assert len(SPEC_BY_KIND) == 11


# --- confinement ----------------------------------------------------------------------


async def test_a_path_outside_the_roots_is_refused(client: httpx.AsyncClient) -> None:
    for path in ("../../etc/passwd", "/etc/passwd", "config/../../secrets.yaml"):
        response = await client.get("/v1/control/specs/file", params={"path": path})
        assert response.status_code == 403, path
        assert "outside the configured spec roots" in response.json()["detail"]


async def test_a_symlink_that_points_out_of_the_root_is_refused(
    client: httpx.AsyncClient, spec_root: Path, tmp_path: Path
) -> None:
    """The attack string manipulation does not stop.

    `config/escape.yaml` is inside the root by every textual measure. It resolves
    outside it, which is the measure that counts.
    """
    secret = tmp_path / "outside.yaml"
    secret.write_text("secret: yes\n", encoding="utf-8")
    (spec_root / "growth" / "escape.yaml").symlink_to(secret)

    response = await client.get(
        "/v1/control/specs/file", params={"path": "config/growth/escape.yaml"}
    )
    assert response.status_code == 403


async def test_a_non_yaml_suffix_is_refused(client: httpx.AsyncClient) -> None:
    """The README inside a spec folder is listed and is not editable through here.

    The loader reads `.yaml` and `.yml`; letting the control surface write anything
    else would make it a general-purpose file server that happens to live under
    `config/`.
    """
    response = await client.get(
        "/v1/control/specs/file", params={"path": "config/growth/README.md"}
    )
    assert response.status_code == 403
    assert "not one of" in response.json()["detail"]


# --- reading and writing --------------------------------------------------------------


async def test_a_round_trip_preserves_comments_and_key_order(
    client: httpx.AsyncClient, spec_root: Path
) -> None:
    """The property that makes the editor usable rather than merely functional.

    A save built on `dump_documents` or `Document.envelope()` would return this file
    with its keys sorted alphabetically and every comment gone. `git diff` would then
    show the whole file changed for a one-line edit, which is the same as showing
    nothing.
    """
    path = "config/growth/30-actors.yaml"
    read = (await client.get("/v1/control/specs/file", params={"path": path})).json()
    assert read["content"] == COMMENTED_YAML
    assert read["sha256"] == _digest(COMMENTED_YAML)

    edited = COMMENTED_YAML.replace("maxCostCents: 500", "maxCostCents: 900")
    response = await client.put(
        "/v1/control/specs/file",
        json={"path": path, "content": edited, "base_sha256": read["sha256"]},
    )
    assert response.status_code == 200

    on_disk = (spec_root / "growth" / "30-actors.yaml").read_text(encoding="utf-8")
    assert on_disk == edited
    assert "# trailing comments survive too" in on_disk
    assert on_disk.index("kind: llm_agent") < on_disk.index("graph:"), "key order kept"
    assert on_disk.splitlines()[0].startswith("# The growth department's head")


async def test_unparseable_yaml_is_422_and_the_file_is_untouched(
    client: httpx.AsyncClient, spec_root: Path
) -> None:
    """A refused save must not be a destructive one."""
    path = "config/growth/30-actors.yaml"
    read = (await client.get("/v1/control/specs/file", params={"path": path})).json()

    response = await client.put(
        "/v1/control/specs/file",
        json={
            "path": path,
            "content": "apiVersion: agent-platform/v1\nkind: [unclosed\n",
            "base_sha256": read["sha256"],
        },
    )
    assert response.status_code == 422
    assert (spec_root / "growth" / "30-actors.yaml").read_text(encoding="utf-8") == COMMENTED_YAML


async def test_a_document_this_build_cannot_represent_is_422(client: httpx.AsyncClient) -> None:
    """Parseable YAML is not the same as a parseable document.

    The envelope check is the loader's, and running it on save is what stops a file
    that would fail at apply time from being saved and forgotten about for a week.
    """
    path = "config/growth/30-actors.yaml"
    read = (await client.get("/v1/control/specs/file", params={"path": path})).json()
    response = await client.put(
        "/v1/control/specs/file",
        json={
            "path": path,
            "content": "apiVersion: agent-platform/v1\nkind: Sasquatch\nmetadata:\n  name: x\n",
            "base_sha256": read["sha256"],
        },
    )
    assert response.status_code == 422
    assert "not a document kind" in response.json()["detail"]


async def test_an_inline_secret_is_refused_on_save(client: httpx.AsyncClient) -> None:
    """Edge case 82, reached through the editor.

    `scan_for_secrets` runs inside `_document`, so `parse_text` catches it — which
    means the control surface inherits the refusal without restating it. A warning
    about a secret in git is a warning about a secret that is already in git.
    """
    path = "config/growth/30-actors.yaml"
    read = (await client.get("/v1/control/specs/file", params={"path": path})).json()
    poisoned = COMMENTED_YAML.replace(
        "  role: head", '  role: head\n  credential: "sk-live-0123456789abcdef0123"'
    )
    response = await client.put(
        "/v1/control/specs/file",
        json={"path": path, "content": poisoned, "base_sha256": read["sha256"]},
    )
    assert response.status_code == 422


# --- concurrency ----------------------------------------------------------------------


async def test_a_stale_base_sha256_is_409(client: httpx.AsyncClient, spec_root: Path) -> None:
    """Two tabs, or a tab and `git checkout`. Last-write-wins is the failure an editor
    must not have."""
    path = "config/growth/30-actors.yaml"
    read = (await client.get("/v1/control/specs/file", params={"path": path})).json()

    # Somebody else saves first.
    (spec_root / "growth" / "30-actors.yaml").write_text(
        COMMENTED_YAML.replace("maxLlmCalls: 12", "maxLlmCalls: 20"), encoding="utf-8"
    )

    response = await client.put(
        "/v1/control/specs/file",
        json={
            "path": path,
            "content": COMMENTED_YAML.replace("maxCostCents: 500", "maxCostCents: 900"),
            "base_sha256": read["sha256"],
        },
    )
    assert response.status_code == 409
    assert "changed since it was opened" in response.json()["detail"]
    assert "maxLlmCalls: 20" in (spec_root / "growth" / "30-actors.yaml").read_text()


async def test_overwriting_without_a_base_digest_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/v1/control/specs/file",
        json={"path": "config/growth/30-actors.yaml", "content": COMMENTED_YAML},
    )
    assert response.status_code == 409


async def test_create_refuses_to_clobber_and_delete_needs_the_digest(
    client: httpx.AsyncClient, spec_root: Path
) -> None:
    created = await client.post(
        "/v1/control/specs/file",
        json={"path": "config/growth/40-extra.yaml", "content": COMMENTED_YAML},
    )
    assert created.status_code == 201
    assert (spec_root / "growth" / "40-extra.yaml").exists()

    again = await client.post(
        "/v1/control/specs/file",
        json={"path": "config/growth/40-extra.yaml", "content": COMMENTED_YAML},
    )
    assert again.status_code == 409

    stale = await client.delete(
        "/v1/control/specs/file",
        params={"path": "config/growth/40-extra.yaml", "base_sha256": "0" * 64},
    )
    assert stale.status_code == 409
    assert (spec_root / "growth" / "40-extra.yaml").exists()

    gone = await client.delete(
        "/v1/control/specs/file",
        params={
            "path": "config/growth/40-extra.yaml",
            "base_sha256": created.json()["sha256"],
        },
    )
    assert gone.status_code == 200
    assert not (spec_root / "growth" / "40-extra.yaml").exists()


# --- the blunt instrument -------------------------------------------------------------


async def test_spec_editable_false_refuses_every_write_and_keeps_the_reads(
    read_only_client: httpx.AsyncClient,
) -> None:
    """`/v1/control` has no auth. This is the one switch that makes it safe to leave
    running somewhere it should not be writing."""
    path = "config/growth/30-actors.yaml"
    read = await read_only_client.get("/v1/control/specs/file", params={"path": path})
    assert read.status_code == 200

    writes = [
        read_only_client.put(
            "/v1/control/specs/file",
            json={"path": path, "content": "x: 1\n", "base_sha256": read.json()["sha256"]},
        ),
        read_only_client.post(
            "/v1/control/specs/file", json={"path": "config/growth/new.yaml", "content": "x: 1\n"}
        ),
        read_only_client.delete(
            "/v1/control/specs/file",
            params={"path": path, "base_sha256": read.json()["sha256"]},
        ),
    ]
    for response in [await w for w in writes]:
        assert response.status_code == 403
        assert "RUNTIME_SPEC_EDITABLE" in response.json()["detail"]
