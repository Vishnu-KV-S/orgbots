"""Fixtures for the M2 governance tests.

Two things live here.

`governed_org` seeds an organization with the real role tree, the real policies, the
real grants and the real rate limits from `runtime.org.governance_seed` — not a
simplified stand-in. A governance test against a hand-built two-role tree would pass
while the shipped configuration was broken, which is the only failure mode that
matters here.

`GatewayHarness` builds a `ToolGateway` with whichever governance services the test
wants and stubs the rest. It exists because the M2 pipeline has six collaborators, and
a test that had to construct all six to assert one of them would be a test nobody
updates.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from runtime.artifacts.store import ArtifactStore
from runtime.budget.service import BudgetService
from runtime.domain.authority import ActionAuthority, ResolvedAuthority
from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import (
    ActorKind,
    AuthorityLevel,
    BlastRadius,
    OnExpiry,
    RecoveryPolicy,
    WorkClass,
)
from runtime.domain.ids import (
    ActorId,
    Fence,
    OrganizationId,
    RunId,
    TaskId,
    new_run_id,
    new_worker_id,
)
from runtime.domain.specs import (
    Ceilings,
    CompiledSpec,
    ModelProfile,
    ModelProfiles,
    RunSpec,
)
from runtime.effects.journal import EffectJournal
from runtime.gateway.credentials import CredentialBroker, CredentialCipher
from runtime.gateway.governance import PermissionCache
from runtime.gateway.ratelimit import RateLimiter
from runtime.gateway.tools import (
    EffectCapabilities,
    ToolContext,
    ToolDef,
    ToolGateway,
    ToolRegistry,
)
from runtime.org.department import DEPARTMENT_SPECS
from runtime.org.governance_seed import DEPARTMENT, IC_ROLE
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.governance_boot import seed_governance
from runtime.settings import Settings

TEST_KEY_ID = "test-k1"
TEST_KEY = base64.b64encode(b"0" * 32).decode()
TEST_KEY_ID_2 = "test-k2"
TEST_KEY_2 = base64.b64encode(b"1" * 32).decode()


def make_cipher() -> CredentialCipher:
    """A cipher with two keys loaded, so key rotation is testable without env games."""
    return CredentialCipher.from_env(
        {
            "RUNTIME_CREDENTIAL_KEYS": f"{TEST_KEY_ID}:{TEST_KEY},{TEST_KEY_ID_2}:{TEST_KEY_2}",
            "RUNTIME_CREDENTIAL_ACTIVE_KEY": TEST_KEY_ID,
        }
    )


async def new_governed_org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    """An organization with the four actors and the **shipped** governance config.

    A plain helper rather than a fixture, matching `conftest_m1.new_org` — only
    `conftest.py` is auto-discovered by pytest, and `conftest.py` deliberately avoids
    importing the gateway so the pure-domain tests stay fast.

    It seeds the real `governance_seed` values, not a simplified stand-in. A
    governance test against a hand-built two-role tree would pass while the shipped
    configuration was broken, which is the only failure mode that matters here.
    """
    organization_id = OrganizationId(uuid.uuid4())
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, "acme")
    for spec in DEPARTMENT_SPECS:
        await registrar.publish_actor(organization_id, spec)
    await seed_governance(
        uow_factory,
        organization_id,
        action_floors={"publish_external": BlastRadius.IRREVERSIBLE},
    )
    return organization_id


async def grant_tools(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    actor: str,
    *tools: str,
) -> None:
    """Give an actor live grants for these tools.

    Needed by any test whose `ResolvedAuthority` carries `tool_grants`, because the
    gateway re-checks them against the live table (edge case 64). Granting for real
    rather than handing the test an authority with an empty grant set — an empty set
    skips the check, which would quietly turn every governance test into one that does
    not exercise it.
    """
    async with uow_factory.transaction() as uow:
        for tool in tools:
            await uow.authority.grant_tool(
                uuid.uuid5(uuid.NAMESPACE_URL, f"grant:{organization_id}:{actor}:{tool}"),
                organization_id,
                subject_type="actor",
                subject_id=actor,
                tool=tool,
            )


# --- a tool that does nothing, for testing the pipeline around it -------------------


class NoopArgs(BaseModel):
    value: str = "x"


class NoopResult(BaseModel):
    echoed: str
    credential_seen: str | None = None


@dataclass
class RecordingTool:
    """Records every call, and can echo back the credential it was handed.

    Echoing the credential is not a convenience — it is how T38 gets a secret into a
    tool *result* without inventing a fake provider that leaks one.

    `during_call` runs after the effect is notionally performed and before the result
    is returned, which is the window the kill-switch tests need: it is the only moment
    where `drain` and `halt` behave differently, and simulating it by substituting the
    registered function would test the substitution.
    """

    calls: list[tuple[ToolContext, NoopArgs]] = field(default_factory=list)
    leak_credential: bool = False
    fail_with: Exception | None = None
    during_call: Callable[[], Awaitable[None]] | None = None
    result_extra: str | None = None

    async def __call__(self, ctx: ToolContext, args: BaseModel) -> BaseModel:
        assert isinstance(args, NoopArgs)
        self.calls.append((ctx, args))
        if self.during_call is not None:
            await self.during_call()
        if self.fail_with is not None:
            raise self.fail_with
        return NoopResult(
            echoed=self.result_extra or args.value,
            credential_seen=ctx.credential if self.leak_credential else None,
        )


def noop_tool_def(
    name: str = "test.noop",
    *,
    blast_radius: BlastRadius = BlastRadius.READ,
    authority_action: str | None = None,
    provider: str | None = None,
) -> ToolDef:
    return ToolDef(
        name=name,
        version=1,
        args_model=NoopArgs,
        result_model=NoopResult,
        capabilities=EffectCapabilities(
            mutates_external_state=False, max_blast_radius=blast_radius
        ),
        recovery_policy=RecoveryPolicy.REPLAY_SAFE,
        authority_action=authority_action,
        provider=provider,
    )


# --- contexts -----------------------------------------------------------------------


def make_authority(
    actor_name: str = "research",
    *,
    role: str | None = IC_ROLE,
    tools: frozenset[str] = frozenset({"test.noop@1"}),
    actions: dict[str, ActionAuthority] | None = None,
    tool_connections: dict[str, str] | None = None,
    connection_credentials: dict[str, str] | None = None,
    approver_budgets: dict[str, int] | None = None,
) -> ResolvedAuthority:
    return ResolvedAuthority(
        actor_name=actor_name,
        role=role,
        department=DEPARTMENT,
        actions=actions or {},
        tool_grants=tools,
        connections=frozenset((tool_connections or {}).values()),
        tool_connections=tool_connections or {},
        connection_credentials=connection_credentials or {},
        approver_budgets=approver_budgets or {},
    )


HUMAN_PUBLISH = ActionAuthority(
    action="publish_external",
    level=AuthorityLevel.HUMAN,
    approver_chain=("director", "operator"),
    max_escalations=1,
    on_expiry=OnExpiry.DENY,
    source="department:marketing",
)


def make_ctx(
    organization_id: OrganizationId,
    *,
    authority: ResolvedAuthority | None = None,
    allowed_tools: frozenset[str] = frozenset({"test.noop@1"}),
    task_id: TaskId | None = None,
    budget_pool_id: str | None = None,
    actor_name: str = "research",
    node: str = "work",
) -> RunContext:
    run_id = new_run_id()
    compiled = CompiledSpec(
        actor_id=ActorId(uuid.uuid4()),
        actor_name=actor_name,
        actor_version=1,
        kind=ActorKind.LLM_AGENT,
        graph_ref="research@1",
        handler_ref=None,
        allowed_tools=allowed_tools,
        ceilings=Ceilings(),
        model_profiles=ModelProfiles(
            profiles={
                WorkClass.WORK: ModelProfile(
                    provider="fake", model="echo-1", input_cents_per_mtok=0
                )
            }
        ),
        allowed_model_call_sites=None,
        authority=authority if authority is not None else make_authority(actor_name),
    )
    spec = RunSpec(
        run_id=run_id,
        organization_id=organization_id,
        root_run_id=run_id,
        task_id=task_id,
        thread_id=str(run_id),
        spec=compiled,
        spec_hash="x" * 64,
        input={},
        budget_pool_id=budget_pool_id,
    )
    ctx = RunContext(
        run_id=run_id,
        organization_id=organization_id,
        root_run_id=run_id,
        actor_id=compiled.actor_id,
        actor_version=1,
        spec=spec,
        lease=Lease(
            run_id=run_id,
            worker_id=new_worker_id(),
            fence=Fence(1),
            lease_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        ),
        worker_id=new_worker_id(),
        trace_id="0" * 32,
    )
    ctx.scope.node = node
    return ctx


@dataclass
class Harness:
    gateway: ToolGateway
    registry: ToolRegistry
    tool: RecordingTool
    kill_switches: KillSwitchService
    permissions: PermissionCache
    credentials: CredentialBroker | None


def build_harness(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    *,
    tool_def: ToolDef | None = None,
    tool: RecordingTool | None = None,
    with_credentials: bool = False,
    rate_limiter: RateLimiter | None = None,
    permission_ttl: float = 30.0,
    kill_ttl: float = 10.0,
) -> Harness:
    registry = ToolRegistry()
    fn = tool or RecordingTool()
    registry.register(tool_def or noop_tool_def(), fn)
    kill = KillSwitchService(uow_factory, ttl_seconds=kill_ttl)
    permissions = PermissionCache(uow_factory, ttl_seconds=permission_ttl)
    broker = CredentialBroker(uow_factory, make_cipher()) if with_credentials else None
    gateway = ToolGateway(
        registry,
        EffectJournal(uow_factory),
        uow_factory,
        artifacts=ArtifactStore(uow_factory, settings=settings),
        budget=BudgetService(),
        settings=settings,
        kill_switches=kill,
        permissions=permissions,
        rate_limiter=rate_limiter,
        credentials=broker,
    )
    return Harness(
        gateway=gateway,
        registry=registry,
        tool=fn,
        kill_switches=kill,
        permissions=permissions,
        credentials=broker,
    )


async def decisions_for(uow_factory: UnitOfWorkFactory, run_id: RunId) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        return await uow.audit.decisions_for_run(run_id)


def env_without_credential_keys() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("RUNTIME_CREDENTIAL")}
