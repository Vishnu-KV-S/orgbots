"""M2 §2/§10 — credentials out of the environment, and secrets out of everything else.

T37 (rotation mid-run, no pinning) and T38 (a secret in a tool result is scrubbed
before state, checkpoint, log and artifact).

M2 §10 risk 5: *"Credentials moving to the database is a real migration. Get rotation
working before you rely on it, and test rotation mid-run rather than assuming it."*
"""

from __future__ import annotations

import json
import uuid

import pytest

from runtime.domain.errors import DecryptionFailed, MissingCredentials
from runtime.domain.ids import OrganizationId
from runtime.domain.scrub import REDACTED, SecretRegistry, registry, scrub, scrub_text
from runtime.gateway.credentials import CredentialBroker, CredentialCipher, fingerprint
from runtime.gateway.tools import ToolCall
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m2 import (
    TEST_KEY_2,
    TEST_KEY_ID,
    TEST_KEY_ID_2,
    RecordingTool,
    build_harness,
    grant_tools,
    make_authority,
    make_cipher,
    make_ctx,
    noop_tool_def,
)

pytestmark = pytest.mark.integration

SECRET_V1 = "sk-live-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA01"
SECRET_V2 = "sk-live-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB02"


@pytest.fixture(autouse=True)
def _clean_registry():  # type: ignore[no-untyped-def]
    """The secret registry is process-global — see the note in `domain/scrub`.

    It has to be, because scrubbing runs in code paths with no `RunContext`. The cost
    is that tests must clean up after each other, which is what this does.
    """
    registry().forget_all()
    yield
    registry().forget_all()


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    await grant_tools(uow_factory, organization_id, "research", "test.noop@1")
    return organization_id


# --- the cipher --------------------------------------------------------------------


def test_a_credential_round_trips() -> None:
    cipher = make_cipher()
    key_id, nonce, ciphertext = cipher.encrypt(SECRET_V1, aad=b"org:name")
    assert key_id == TEST_KEY_ID
    assert SECRET_V1.encode() not in ciphertext
    assert cipher.decrypt(key_id, nonce, ciphertext, aad=b"org:name") == SECRET_V1


def test_the_aad_binds_a_row_to_its_organization() -> None:
    """A row copied from one organization's table to another's must fail to open,
    not silently authenticate as somebody else."""
    cipher = make_cipher()
    key_id, nonce, ciphertext = cipher.encrypt(SECRET_V1, aad=b"org-a:search_api_key")
    with pytest.raises(DecryptionFailed, match="moved between organizations"):
        cipher.decrypt(key_id, nonce, ciphertext, aad=b"org-b:search_api_key")


def test_a_key_this_process_does_not_have_is_a_loud_deployment_fault() -> None:
    """Not a data fault — falling back to an empty credential would turn a
    misconfigured deploy into a mysterious provider error hours later."""
    two_keys = make_cipher()
    _, nonce, ciphertext = two_keys.encrypt(SECRET_V1, aad=b"a")

    one_key = CredentialCipher.from_env(
        {"RUNTIME_CREDENTIAL_KEYS": f"{TEST_KEY_ID_2}:{TEST_KEY_2}"}
    )
    with pytest.raises(DecryptionFailed, match="deployment fault"):
        one_key.decrypt(TEST_KEY_ID, nonce, ciphertext, aad=b"a")


def test_the_cipher_refuses_to_invent_a_key() -> None:
    """A process that generated its own would encrypt rows nothing else can read, and
    the failure would surface on a different host."""
    with pytest.raises(MissingCredentials, match="RUNTIME_CREDENTIAL_KEYS"):
        CredentialCipher.from_env({})


def test_nonces_are_never_reused_across_rotations() -> None:
    """A repeated nonce is the one way to break GCM outright."""
    cipher = make_cipher()
    nonces = {cipher.encrypt(SECRET_V1, aad=b"a")[1] for _ in range(50)}
    assert len(nonces) == 50


def test_a_credential_does_not_appear_in_its_own_repr() -> None:
    """A traceback is the most widely copied artifact there is."""
    from runtime.gateway.credentials import Credential

    cred = Credential("k", "p", 1, SECRET_V1, fingerprint(SECRET_V1))
    assert SECRET_V1 not in repr(cred)
    assert fingerprint(SECRET_V1) in repr(cred)


# --- rotation ------------------------------------------------------------------------


async def test_rotation_retires_the_previous_version(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = await _org(uow_factory)
    broker = CredentialBroker(uow_factory, make_cipher())

    assert (
        await broker.put(
            organization_id, name="search_api_key", provider="serper", secret=SECRET_V1
        )
        == 1
    )
    assert (
        await broker.put(
            organization_id, name="search_api_key", provider="serper", secret=SECRET_V2
        )
        == 2
    )

    async with uow_factory() as uow:
        rows = await uow.credentials.list_for_org(organization_id)
    assert [(r.version, r.status.value) for r in rows] == [(2, "ACTIVE"), (1, "RETIRED")]
    assert (await broker.fetch(organization_id, "search_api_key")).secret == SECRET_V2


async def test_t37_a_rotation_mid_run_is_picked_up_by_the_next_call(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """v3 edge 65. The credential is fetched per call and never pinned onto the run.

    This is the test M2 §10 asks for by name: *test rotation mid-run rather than
    assuming it*. A broker that cached — even for a few seconds — would pass a test
    that rotated between runs and fail this one, and the whole reason to have rotation
    is that a compromised key must stop working now.
    """
    organization_id = await _org(uow_factory)
    tool = RecordingTool(leak_credential=True)
    harness = build_harness(
        uow_factory,
        settings,
        tool=tool,
        tool_def=noop_tool_def(provider="serper"),
        with_credentials=True,
    )
    assert harness.credentials is not None
    await harness.credentials.put(
        organization_id, name="search_api_key", provider="serper", secret=SECRET_V1
    )

    authority = make_authority(
        tool_connections={"test.noop@1": "search-primary"},
        connection_credentials={"search-primary": "search_api_key"},
    )
    ctx = make_ctx(organization_id, authority=authority)

    await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "before"}))
    assert tool.calls[-1][0].credential == SECRET_V1
    assert tool.calls[-1][0].connection == "search-primary"

    # Rotate while the same run is still in flight — same ctx, same lease, same fence.
    await harness.credentials.put(
        organization_id, name="search_api_key", provider="serper", secret=SECRET_V2
    )

    await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "after"}))
    assert tool.calls[-1][0].credential == SECRET_V2, "no pinning: the next call re-reads"


async def test_a_tool_whose_credential_is_missing_is_refused_clearly(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A tool that silently proceeds without its key produces a failure at the
    provider that looks like the provider's fault."""
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings, with_credentials=True)
    authority = make_authority(
        tool_connections={"test.noop@1": "search-primary"},
        connection_credentials={"search-primary": "search_api_key"},
    )
    ctx = make_ctx(organization_id, authority=authority)

    with pytest.raises(MissingCredentials, match="search_api_key"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))


async def test_a_gateway_with_no_broker_refuses_rather_than_running_uncredentialed(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings, with_credentials=False)
    authority = make_authority(
        tool_connections={"test.noop@1": "search-primary"},
        connection_credentials={"search-primary": "search_api_key"},
    )
    ctx = make_ctx(organization_id, authority=authority)

    with pytest.raises(MissingCredentials, match="no credential broker"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))


async def test_a_tool_that_needs_no_credential_gets_none(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Most tools need nothing. Inventing a credential for them would make "no
    credential" indistinguishable from "the credential is missing"."""
    organization_id = await _org(uow_factory)
    tool = RecordingTool(leak_credential=True)
    harness = build_harness(uow_factory, settings, tool=tool, with_credentials=True)
    ctx = make_ctx(organization_id, authority=make_authority())

    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))
    assert result.ok
    assert tool.calls[-1][0].credential is None


# --- T38: scrubbing --------------------------------------------------------------------


def test_the_registry_redacts_by_exact_match() -> None:
    """The mechanism that actually works. The patterns below are a backstop."""
    reg = SecretRegistry()
    reg.register(SECRET_V1, label="credential:search")
    text, hits = reg.scrub(f"the provider said: {SECRET_V1} was rejected")
    assert SECRET_V1 not in text
    assert REDACTED in text
    assert hits == ["credential:search"]


def test_a_short_value_is_not_registered() -> None:
    """Below eight characters, exact-match redaction would eat ordinary substrings."""
    reg = SecretRegistry()
    reg.register("abc", label="tiny")
    text, hits = reg.scrub("abc is a common substring in abcdef")
    assert text == "abc is a common substring in abcdef"
    assert hits == []


def test_the_registry_holds_digests_rather_than_plaintext() -> None:
    """A heap dump of the registry must not be a second copy of the credentials."""
    reg = SecretRegistry()
    reg.register(SECRET_V1, label="credential:search")
    assert SECRET_V1 not in repr(reg.__dict__)
    assert reg.label_for(SECRET_V1) == "credential:search"


def test_the_pattern_backstop_catches_other_peoples_secrets() -> None:
    """Guesses, and labelled as such — for a bearer token in a fetched page, not ours."""
    text, hits = scrub_text(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n"
        "aws key AKIAIOSFODNN7EXAMPLE\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    )
    assert "abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "MIIabc" not in text
    assert set(hits) == {"bearer_header", "aws_access_key", "private_key"}


def test_scrubbing_walks_keys_as_well_as_values() -> None:
    """A provider that echoes a request back with the key as a *field name* is not
    hypothetical, and a scrubber that only looked at values would write it out."""
    registry().register(SECRET_V1, label="credential:search")
    scrubbed, hits = scrub({SECRET_V1: {"nested": [SECRET_V1]}})
    assert SECRET_V1 not in json.dumps(scrubbed)
    assert len(hits) == 2


async def test_t38_a_secret_in_a_tool_result_never_reaches_state_or_the_artifact_store(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """T38's four destinations: state, checkpoint, log, artifact.

    The gateway scrubs immediately after the effect and before anything writes the
    payload anywhere, so all four are downstream of one call. The tool here echoes the
    credential it was handed, which is how a real provider leaks one — in an error
    message quoting the request.
    """
    organization_id = await _org(uow_factory)
    tool = RecordingTool(leak_credential=True)
    harness = build_harness(
        uow_factory,
        settings,
        tool=tool,
        tool_def=noop_tool_def(provider="serper"),
        with_credentials=True,
    )
    assert harness.credentials is not None
    await harness.credentials.put(
        organization_id, name="search_api_key", provider="serper", secret=SECRET_V1
    )

    authority = make_authority(
        tool_connections={"test.noop@1": "search-primary"},
        connection_credentials={"search-primary": "search_api_key"},
    )
    ctx = make_ctx(organization_id, authority=authority)
    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    # 1. The value returned into graph state.
    assert SECRET_V1 not in json.dumps(result.value)
    assert result.value["credential_seen"] == REDACTED

    # 2. The effect journal, which is what a replay reads back — and therefore what
    #    ends up in the checkpoint on the attempt that resumes.
    async with uow_factory() as uow:
        effects = await uow.effects.for_run(ctx.run_id)
        decisions = await uow.audit.decisions_for_run(ctx.run_id)
    assert SECRET_V1 not in json.dumps(effects[0].result_inline)

    # 3. The audit trail records *that* a secret was scrubbed, not the secret.
    scrubs = [d for d in decisions if d["check"] == "secret_scrub"]
    assert len(scrubs) == 1
    assert "credential:search_api_key@v1" in (scrubs[0]["reason"] or "")
    assert SECRET_V1 not in json.dumps(decisions)


async def test_t38_a_secret_large_enough_to_be_externalised_is_scrubbed_first(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The artifact destination specifically. Scrubbing has to be upstream of the
    size check, or a big result takes the secret to the object store."""
    organization_id = await _org(uow_factory)
    big = "y" * (settings.artifact_threshold_bytes + 1_000)
    tool = RecordingTool(leak_credential=True, result_extra=f"{big}{SECRET_V1}")
    harness = build_harness(
        uow_factory,
        settings,
        tool=tool,
        tool_def=noop_tool_def(provider="serper"),
        with_credentials=True,
    )
    assert harness.credentials is not None
    await harness.credentials.put(
        organization_id, name="search_api_key", provider="serper", secret=SECRET_V1
    )
    authority = make_authority(
        tool_connections={"test.noop@1": "search-primary"},
        connection_credentials={"search-primary": "search_api_key"},
    )
    ctx = make_ctx(organization_id, authority=authority)

    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    assert result.artifact is not None, "big enough to be externalised"
    stored = await harness.gateway._artifacts.get(result.artifact.artifact_id)
    assert SECRET_V1.encode() not in stored
    assert REDACTED.encode() in stored


# --- the model provider's credential ---------------------------------------------------


async def test_the_model_provider_is_fed_from_the_store_and_rebuilds_on_rotation(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§1: *"Credentials moved out of env into an encrypted table with per-call fetch."*

    A model provider holds a credential like anything else. The gateway fetches per
    call; the provider rebuilds its client only when the *fingerprint* changes — which
    keeps prompt caching's stable prefix intact between rotations and throws it away
    exactly when a rotation means it must.
    """
    from dataclasses import dataclass, field

    from runtime.domain.enums import WorkClass
    from runtime.gateway.models import ModelGateway, ModelRequest, ModelResponse

    @dataclass
    class RecordingProvider:
        name: str = "deepseek"
        applied: list[str] = field(default_factory=list)
        rebuilds: int = 0
        _fingerprint: str | None = None

        def use_credential(self, secret: str, *, fingerprint: str) -> None:
            self.applied.append(secret)
            if fingerprint != self._fingerprint:
                self.rebuilds += 1
                self._fingerprint = fingerprint

        async def complete(self, profile, req):  # type: ignore[no-untyped-def]
            return ModelResponse(
                text="ok",
                provider=profile.provider,
                model=profile.model,
                input_tokens=1,
                output_tokens=1,
                cost_cents=0,
            )

    organization_id = await _org(uow_factory)
    provider = RecordingProvider()
    broker = CredentialBroker(uow_factory, make_cipher())
    await broker.put(
        organization_id, name="deepseek_api_key", provider="deepseek", secret=SECRET_V1
    )

    gateway = ModelGateway(
        uow_factory,
        providers={"deepseek": provider},
        settings=settings,
        credentials=broker,
    )
    authority = make_authority()
    ctx = make_ctx(organization_id, authority=authority)
    from runtime.domain.specs import ModelProfile, ModelProfiles

    model_spec = ctx.spec.spec.model_copy(
        update={
            "model_profiles": ModelProfiles(
                profiles={
                    WorkClass.WORK: ModelProfile(provider="deepseek", model="deepseek-v4-pro")
                }
            )
        }
    )
    ctx.spec = ctx.spec.model_copy(update={"spec": model_spec})

    for _ in range(3):
        await gateway.complete(
            ctx, ModelRequest(prompt="hi"), work_class=WorkClass.WORK, call_site="t"
        )
    assert provider.applied == [SECRET_V1] * 3, "fetched per call"
    assert provider.rebuilds == 1, "and the client is reused between rotations"

    await broker.put(
        organization_id, name="deepseek_api_key", provider="deepseek", secret=SECRET_V2
    )
    await gateway.complete(ctx, ModelRequest(prompt="hi"), work_class=WorkClass.WORK, call_site="t")
    assert provider.applied[-1] == SECRET_V2
    assert provider.rebuilds == 2, "a rotation must invalidate the client, not reuse it"


async def test_a_provider_with_no_stored_credential_keeps_its_own(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The development path, left open deliberately.

    An absent row means "use whatever you resolved for yourself" — an env var, a local
    auth profile. Requiring a credentials table to run the test suite would be a
    governance mechanism that made the thing harder to work on, which is how governance
    gets switched off.
    """
    from runtime.domain.enums import WorkClass
    from runtime.gateway.models import ModelGateway, ModelRequest

    organization_id = await _org(uow_factory)
    gateway = ModelGateway(
        uow_factory,
        settings=settings,
        credentials=CredentialBroker(uow_factory, make_cipher()),
    )
    ctx = make_ctx(organization_id, authority=make_authority())

    response = await gateway.complete(
        ctx, ModelRequest(prompt="hi"), work_class=WorkClass.WORK, call_site="t"
    )
    assert response.text.startswith("echo:"), "the echo provider is not credential-backed"


async def test_a_model_completion_is_scrubbed_too(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A completion is text from outside: the model was shown a key in an error
    message, or repeated one out of a fetched page."""
    from runtime.domain.enums import WorkClass
    from runtime.gateway.models import ModelGateway, ModelRequest

    organization_id = await _org(uow_factory)
    registry().register(SECRET_V1, label="credential:search_api_key@v1")
    gateway = ModelGateway(uow_factory, settings=settings)
    ctx = make_ctx(organization_id, authority=make_authority())

    response = await gateway.complete(
        ctx,
        ModelRequest(prompt=f"repeat this: {SECRET_V1}"),
        work_class=WorkClass.WORK,
        call_site="test",
    )
    assert SECRET_V1 not in response.text
    assert REDACTED in response.text
