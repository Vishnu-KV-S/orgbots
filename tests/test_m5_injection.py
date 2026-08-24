"""T70 — M2's injection corpus, re-run with delegation on.

§6: *"T70 matters because delegation is a new privilege-escalation surface: an injected
instruction that persuades a parent to delegate with wider scope. Checks 5 and 6 in §4
are the defence; T70 is the proof."*

The corpus is M2's, unchanged — the same twenty payloads, loaded from the same
`tests/fixtures/injection` manifest — because a corpus rewritten for the feature under
test is a corpus that tests the feature it was written for. What is new is the
**ingress**. M2 ran every payload through four (`web_fetch`, `inbox`, `artifact`,
`task_input`); delegation adds a fifth, and it is the most dangerous shape yet, because
the thing crossing it is not content but a *request to create a run*.

Three claims, one per section below:

**The payload is carried, not obeyed.** A child that silently dropped the parent's
facts would pass an escalation test while being useless, so the payload must be present
in the child's input — as data, at the same fenced ingress M2 already measured.

**Nothing a payload can say reaches a governing field.** Authority, memory scopes,
tools, depth and path all come from the parent's frozen spec and the child's own actor
row. The payload reaches none of them, and the test asserts that per payload rather
than in aggregate, so a failure names the one that got through.

**A widened request is refused whatever the payload says.** The escalation the corpus
is actually attempting, spelled as the delegation the compromised parent would make.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from runtime.domain.authority import ActionAuthority, ResolvedAuthority
from runtime.domain.enums import AuthorityLevel, MemoryScope, OnExpiry
from runtime.domain.errors import PrivilegeEscalation, ScopeEscalation
from runtime.domain.ids import OrganizationId
from runtime.domain.trust import UntrustedBlock, contains_unfenced
from runtime.graphs.common.context import assemble
from tests.conftest_m5 import CHILD, PARENT, Tree, build_tree, delegating_settings
from tests.test_m2_injection import IDS, PAYLOADS, Payload

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def tree(settings, organization_id: OrganizationId):
    async for built in build_tree(delegating_settings(settings), organization_id):
        yield built


def _delegated(tree: Tree, parent_run, payload: Payload, ordinal: int):
    """The child request a compromised parent would make: payload everywhere it can go.

    Title, objective, task input and facts — every field of `ChildContext` that carries
    a string. If any of them were a route into a governing field, this is the request
    that would find it.
    """
    from tests.test_m5_delegation import _child_request_for

    request = _child_request_for(tree, parent_run, CHILD, ordinal)
    return request.model_copy(
        update={
            "input": {
                "message": payload.body,
                "_delegation": {
                    "facts": [payload.body, payload.marker],
                    "title": payload.body[:200],
                    "objective": payload.body[:200],
                },
            }
        }
    )


# --- claim 1: carried, at a fenced ingress ---------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_a_delegated_fact_reaches_the_child_prompt_inside_a_fence(payload: Payload) -> None:
    """The delegation boundary reuses M2's fencing rather than routing around it.

    A parent's extracted facts came from the parent's own context, which may itself
    have been untrusted — §6's whole point is that you cannot reliably tell which
    extracted sentence came from which block. So a fact crossing into a child is
    untrusted content at a new ingress, and the answer is the answer M2 already built:
    fence it, and let `contains_unfenced` decide whether the fence held.

    `contains_unfenced` treats a fence whose closing nonce does not match its opening
    one as *not a fence*, which is what keeps payload 11 — the fence escape — a real
    test rather than one that agrees with the attacker.
    """
    import uuid as _uuid

    from tests.conftest_m2 import make_ctx

    context = assemble(
        system_prompt="You are a child actor executing one delegated task.",
        spec=make_ctx(OrganizationId(_uuid.uuid4())).spec,
        task_input={"delegated_facts": "see the untrusted block"},
        instruction="Do the task.",
        untrusted=[
            UntrustedBlock(
                source="delegation:facts",
                content=payload.body,
                ingress="delegation",
            )
        ],
    )
    assert payload.marker in context.prompt, "the fact must be carried, not dropped"
    assert not contains_unfenced(context.prompt, payload.marker), (
        f"{payload.name} reached instruction position through the delegation ingress"
    )


# --- claim 2: nothing a payload says reaches a governing field ---------------------------


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
async def test_no_payload_widens_the_child_it_is_carried_into(tree: Tree, payload: Payload) -> None:
    """T70's core. One admission per payload, and the assertions are per payload.

    The governing fields are enumerated rather than spot-checked, because the failure
    this is looking for is *one* of them becoming reachable and an aggregate assertion
    would hide which. Every one of them is resolved from the parent's frozen spec or the
    child's own actor row; none of them has a code path that reads the request's input.
    """
    parent_run = await tree.start(PARENT, f"t70-{payload.name}", {})
    result = await tree.service.start_run(_delegated(tree, parent_run, payload, 0))
    assert result.admitted, "a carried payload is data; it does not refuse the run"

    async with tree.uow() as uow:
        row = await uow.runs.get_spec(result.run_id)
    assert row is not None
    spec = row.spec

    assert spec["spec"]["actor_name"] == CHILD
    assert spec["spec"]["allowed_tools"] == [], "the child's tools are its own, and it has none"
    assert spec["spec"]["authority"]["tool_grants"] == []
    assert spec["spec"]["authority"]["default_level"] == "denied", "default-deny survives"
    # Not "no actions": the child resolves `publish_external` to **denied**, because
    # the blast-radius floor raises an irreversible action to HUMAN and then, finding
    # no approver, to DENIED. That entry is M2's floor working, and asserting an empty
    # dict here would have made this test fail for the right system doing the right
    # thing. What matters is that every entry is denied.
    levels = {a["level"] for a in spec["spec"]["authority"]["actions"].values()}
    assert levels <= {"denied"}, f"a payload bought a non-denied action: {levels}"
    assert spec["memory_scopes"] is None, "unrestricted parent, unrestricted child"
    assert spec["agent_path"] == [PARENT, CHILD]
    assert spec["depth"] == 1
    assert spec["session_id"] is None
    assert spec["delegation"] is None, "the child cannot delegate; it has no block"

    # And the payload *is* there, in the one place it belongs.
    assert payload.marker in str(spec["input"]), "the payload must be carried as data"


# --- claim 3: the escalation the corpus is attempting, spelled as a delegation ------------


@pytest.mark.parametrize(
    "payload",
    [p for p in PAYLOADS if p.attack in {"authority_escalation", "approval_bypass"}],
    ids=[p.name for p in PAYLOADS if p.attack in {"authority_escalation", "approval_bypass"}],
)
async def test_an_authority_escalation_payload_cannot_buy_a_wider_child(
    tree: Tree, payload: Payload
) -> None:
    """Check 5, against the request a fully compromised parent would construct.

    The model does not build a `StartRunRequest` — it returns a plan, and the runtime
    builds the request from the parent's frozen spec. So the strongest possible attack
    is not "the model asked for more authority" but "assume the model somehow *did*",
    and that is what this constructs directly: a child whose resolved authority is
    genuinely wider than the parent's. It is refused at admission, before a run exists.
    """
    parent_run = await tree.start(PARENT, f"t70-auth-{payload.name}", {})
    request = _delegated(tree, parent_run, payload, 0)
    escalated = request.model_copy(
        update={
            "parent_authority": ResolvedAuthority(
                actor_name=PARENT,
                actions={
                    "publish_external": ActionAuthority(
                        action="publish_external", level=AuthorityLevel.DENIED
                    )
                },
            ),
        }
    )
    import uuid as _uuid

    async with tree.uow.transaction() as uow:
        # `research` rather than `publish_external`: an actor-scoped `auto` on an
        # IRREVERSIBLE action is raised to HUMAN by the blast-radius floor and then to
        # DENIED for want of an approver, so both sides end up denied and the subset
        # holds. The floor is doing its job; the escalation worth testing is one it
        # cannot see. `test_a_child_with_wider_authority_is_refused` records the same
        # finding at more length.
        await uow.authority.upsert_policy(
            _uuid.uuid4(),
            tree.organization_id,
            scope_type="actor",
            scope_id=CHILD,
            action="research",
            level=AuthorityLevel.AUTO,
            approver_role=None,
            max_escalations=0,
            on_expiry=OnExpiry.DENY,
            ttl_seconds=3600,
        )

    with pytest.raises(PrivilegeEscalation):
        await tree.service.start_run(escalated)

    async with tree.uow() as uow:
        assert await uow.runs.live_children(parent_run) == 0


@pytest.mark.parametrize(
    "payload",
    [p for p in PAYLOADS if p.attack == "scope_escalation"],
    ids=[p.name for p in PAYLOADS if p.attack == "scope_escalation"],
)
async def test_a_scope_escalation_payload_cannot_buy_a_wider_child(
    tree: Tree, payload: Payload
) -> None:
    """Check 6, edge cases 44, 45 and 81, against the same compromised parent.

    Both spellings: asking for a wider scope, and asking for the default under a parent
    that was narrowed. The second is the one that looks innocent in a diff.
    """
    parent_run = await tree.start(PARENT, f"t70-scope-{payload.name}", {})
    request = _delegated(tree, parent_run, payload, 0)

    with pytest.raises(ScopeEscalation):
        await tree.service.start_run(
            request.model_copy(
                update={
                    "parent_memory_scopes": (MemoryScope.PRIVATE_ACTOR,),
                    "memory_scopes": (MemoryScope.COMPANY,),
                }
            )
        )
    with pytest.raises(ScopeEscalation):
        await tree.service.start_run(
            request.model_copy(
                update={
                    "parent_memory_scopes": (MemoryScope.PRIVATE_ACTOR,),
                    "memory_scopes": None,
                }
            )
        )

    async with tree.uow() as uow:
        assert await uow.runs.live_children(parent_run) == 0
        assert await uow.delegations.for_parent(parent_run) == []
