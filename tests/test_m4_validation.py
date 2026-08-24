"""§6 — every semantic check, positive and negative.

§12's hard exit criterion: *"Every §6 validation has a positive and a negative test."*
The positive case is one test — the exported department validates — plus, for the
checks where "the valid shape" is not obvious, an explicit one.

Each negative test is **one edit** away from a valid corpus. That is the whole design
of `tests.conftest_m4.Corpus`: a negative test that had to build a document set from
scratch would be testing its own scaffolding as much as the check.

No database. Every check here is a property of the documents.
"""

from __future__ import annotations

import pytest

from runtime.domain.errors import EscalationCycle
from runtime.spec.errors import SpecValidationError
from tests.conftest_m4 import TEST_REGISTRIES, Corpus


def valid() -> Corpus:
    return Corpus.department()


# --- the positive case ------------------------------------------------------------------


def test_the_exported_department_validates() -> None:
    """The one positive case that covers every check at once: a real organization."""
    org = valid().validate()
    assert len(org.actors) == 4
    assert org.budget is not None


def test_validation_without_a_registry_checks_structure_only() -> None:
    """`--no-registry`. A CI job with no imported graphs still catches a dangling
    department; it just cannot know whether `research@1` exists."""
    from runtime.spec.validation import Registries, validate

    org = valid().edit("Actor", "research", graph="nonexistent@9").compile()
    validate(org, registries=Registries())
    with pytest.raises(SpecValidationError, match="not registered"):
        validate(org, registries=TEST_REGISTRIES)


# --- referenced department exists ----------------------------------------------------------


def test_a_dangling_department_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="no Department document"):
        valid().edit("Actor", "research", department="finance").validate()


def test_an_actor_with_no_department_is_fine() -> None:
    """Not every actor belongs to one, and the budget chain already handles it."""
    valid().edit("Actor", "research", department=None, memory=None).edit(
        "BudgetPolicy", "default", actors={}
    ).validate()


def test_a_department_head_that_is_not_an_actor_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="head"):
        valid().edit("Department", "marketing", head="nobody").validate()


# --- reportsTo -------------------------------------------------------------------------------


def test_reports_to_must_resolve() -> None:
    with pytest.raises(SpecValidationError, match="reportsTo"):
        valid().edit("Actor", "research", reportsTo="nobody").validate()


def test_an_actor_may_not_report_to_itself() -> None:
    with pytest.raises(SpecValidationError, match="reports to itself"):
        valid().edit("Actor", "research", reportsTo="research").validate()


def test_the_reporting_graph_must_be_acyclic() -> None:
    """A cycle here is an infinite escalation."""
    with pytest.raises(SpecValidationError, match="reporting graph contains a cycle"):
        valid().edit("Actor", "marketing-head", reportsTo="research").validate()


def test_a_reporting_chain_is_fine() -> None:
    valid().edit("Actor", "content", reportsTo="research").validate()


# --- roles ------------------------------------------------------------------------------------


def test_a_dangling_role_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="no Role document"):
        valid().edit("Actor", "research", role="wizard").validate()


def test_a_role_that_is_its_own_parent_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="its own parent"):
        valid().edit("Role", "ic", parent="ic").validate()


# --- graph / handler registry -------------------------------------------------------------------


def test_an_unregistered_graph_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="graph 'nowhere@1' is not registered"):
        valid().edit("Actor", "research", graph="nowhere@1").validate()


def test_an_unregistered_handler_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="handler 'nowhere@1' is not registered"):
        valid().edit("Actor", "analytics", handler="nowhere@1").validate()


def test_a_registered_entrypoint_passes() -> None:
    valid().edit("Actor", "research", graph="content@1").validate()


# --- tools ---------------------------------------------------------------------------------------


def test_a_tool_that_does_not_exist_at_that_version_is_refused() -> None:
    """The pinned version is the point: `web.search@1` and `@2` are different tools."""
    corpus = valid()
    corpus.edit("Actor", "research", tools=["web.search@2", "web.fetch@1"])
    corpus.find("ToolGrant", "actor-research-web-search-1")["spec"]["tool"] = "web.search@2"
    with pytest.raises(SpecValidationError, match="not registered at that version"):
        corpus.validate()


def test_a_tool_in_the_spec_with_no_grant_is_refused() -> None:
    """M2's rule, said at compile time. `allowed_tools` and `tool_grants` answer
    different questions and disagreement between them is a bug."""
    with pytest.raises(SpecValidationError, match="no ToolGrant document grants them"):
        valid().drop("ToolGrant", "actor-research-web-fetch-1").validate()


def test_a_grant_with_no_matching_spec_entry_is_allowed() -> None:
    """Only one direction is an error: a dead grant is refused by the spec check first."""
    valid().add(
        "ToolGrant",
        "actor-content-search",
        {"subject": {"actor": "content"}, "tool": "web.search@1"},
    ).validate()


def test_a_grant_for_an_unknown_subject_is_refused() -> None:
    corpus = valid()
    corpus.find("ToolGrant", "actor-research-web-fetch-1")["spec"]["subject"] = {"actor": "ghost"}
    with pytest.raises(SpecValidationError, match="not defined in this document set"):
        corpus.validate()


def test_a_grant_naming_an_unknown_connection_is_refused() -> None:
    corpus = valid()
    corpus.find("ToolGrant", "actor-research-web-search-1")["spec"]["connection"] = "nope"
    with pytest.raises(SpecValidationError, match="no Connection document"):
        corpus.validate()


def test_provider_side_search_needs_the_search_grant() -> None:
    """Two keys open that door and a model profile is only one of them: a provider-side
    search never reaches the tool gateway, so it may not reach further than the front
    door would."""
    corpus = valid().drop("ToolGrant", "actor-research-web-search-1")
    corpus.edit("Actor", "research", tools=["web.fetch@1"])
    with pytest.raises(SpecValidationError, match="provider-side webSearch"):
        corpus.validate()


# --- memory scopes (edge case 81) -------------------------------------------------------------


def test_an_actor_may_name_its_own_department_scope() -> None:
    valid().edit(
        "Actor", "research", memory={"scopes": ["private", "dept:marketing", "company"]}
    ).validate()


def test_an_actor_may_not_name_another_departments_scope() -> None:
    """Scope escalation by config edit. The retrieval path intersects rather than
    unions, so this could not actually widen anything — which is exactly why it must be
    refused loudly here rather than dropped silently there."""
    corpus = valid().add("Department", "finance", {"description": "", "head": None, "parent": None})
    corpus.edit("Actor", "research", memory={"scopes": ["dept:finance"]})
    with pytest.raises(SpecValidationError, match="not this actor's department"):
        corpus.validate()


def test_a_department_scope_needs_a_department() -> None:
    corpus = valid().edit("Actor", "research", department=None, memory={"scopes": ["department"]})
    corpus.edit("BudgetPolicy", "default", actors={})
    with pytest.raises(SpecValidationError, match="belongs to no department"):
        corpus.validate()


def test_an_unknown_scope_token_is_refused() -> None:
    from runtime.spec.errors import SpecDocumentError

    with pytest.raises(SpecDocumentError, match="is not a memory scope"):
        valid().edit("Actor", "research", memory={"scopes": ["everything"]}).validate()


# --- deterministic workers ------------------------------------------------------------------------


def test_a_deterministic_worker_may_not_hold_model_profiles() -> None:
    """The `max_llm_calls = 0` guarantee, at compile. `analytics` is the control actor
    the M1 numbers are read off; a profile here would quietly make it a fifth LLM
    agent."""
    from runtime.spec.errors import SpecDocumentError

    with pytest.raises(SpecDocumentError, match="may not declare modelProfiles"):
        valid().edit("Actor", "analytics", modelProfiles={"work": "deepseek-v4-pro"}).validate()


def test_a_deterministic_worker_must_have_a_zero_llm_ceiling() -> None:
    from runtime.spec.errors import SpecDocumentError

    corpus = valid()
    ceilings = dict(corpus.find("Actor", "analytics")["spec"]["ceilings"])
    ceilings["maxLlmCalls"] = 4
    with pytest.raises(SpecDocumentError, match="maxLlmCalls == 0"):
        corpus.edit("Actor", "analytics", ceilings=ceilings).validate()


def test_a_deterministic_worker_as_exported_is_valid() -> None:
    org = valid().validate()
    analytics = org.actor("analytics")
    assert analytics is not None
    assert analytics.spec.ceilings.max_llm_calls == 0
    assert not analytics.spec.model_profiles.profiles


# --- HYBRID ---------------------------------------------------------------------------------------


def test_hybrid_is_refused() -> None:
    """Refusing to compile is a stronger guarantee than a runtime check a future edit
    could route around, and the config plane must not become the route around it."""
    corpus = valid().edit("Actor", "research", kind="hybrid", allowedModelCallSites=["plan"])
    with pytest.raises(SpecValidationError, match="kind hybrid is not implemented"):
        corpus.validate()


# --- authority (M2 T32, re-run against config) --------------------------------------------------


def test_an_escalation_cycle_is_refused() -> None:
    """A role tree that eats its own tail. At runtime this is two runs each waiting for
    the other's approver; here it is a graph walk that costs nothing."""
    with pytest.raises(EscalationCycle):
        valid().edit("Role", "operator", parent="ic", rank=30).validate()


def test_rank_must_agree_with_the_tree() -> None:
    with pytest.raises(EscalationCycle, match="disagree about who is senior"):
        valid().edit("Role", "ic", rank=1).validate()


def test_a_role_may_not_approve_for_itself() -> None:
    corpus = valid().add(
        "AuthorityPolicy",
        "role-ic-publish",
        {
            "scope": "role",
            "target": "ic",
            "action": "publish_social",
            "level": "human",
            "approverRole": "ic",
        },
    )
    with pytest.raises(EscalationCycle, match="approves for itself"):
        corpus.validate()


def test_an_approver_must_be_up_hierarchy() -> None:
    corpus = valid().add(
        "AuthorityPolicy",
        "role-head-publish",
        {
            "scope": "role",
            "target": "head",
            "action": "publish_social",
            "level": "human",
            "approverRole": "ic",
        },
    )
    with pytest.raises(EscalationCycle, match="up-hierarchy"):
        corpus.validate()


def test_a_department_policy_naming_a_junior_approver_governs_nobody() -> None:
    """M2's `_subject_roles`: a department policy governs the roles *below* its
    approver. Naming `ic` leaves no subjects, which is useless rather than illegal —
    and the resolver agrees, so the actor falls through to the blast-radius floor and
    is denied. That is the signal to name somebody further up."""
    corpus = valid()
    corpus.find("AuthorityPolicy", "department-marketing-publish-external")["spec"][
        "approverRole"
    ] = "ic"
    corpus.validate()


def test_an_approver_role_that_does_not_exist_is_refused() -> None:
    corpus = valid()
    corpus.find("AuthorityPolicy", "department-marketing-publish-external")["spec"][
        "approverRole"
    ] = "chancellor"
    with pytest.raises(EscalationCycle, match="does not exist"):
        corpus.validate()


def test_inline_actor_authority_compiles_to_an_actor_scoped_policy() -> None:
    org = (
        valid()
        .edit(
            "Actor",
            "marketing-head",
            authority={"approvalRequired": {"publish_social": "human"}, "approverRole": "director"},
        )
        .validate()
    )
    policy = next(
        p for p in org.policies if p.scope_type == "actor" and p.action == "publish_social"
    )
    assert policy.approver_role == "director"


def test_inline_actor_authority_is_checked_for_up_hierarchy_too() -> None:
    """M2's row-level check cannot do this one — `_subject_roles` returns nothing for an
    actor scope because the actor's role is not in the role rows. The compiler knows
    it."""
    corpus = valid().edit(
        "Actor",
        "marketing-head",
        authority={"approvalRequired": {"publish_social": "human"}, "approverRole": "ic"},
    )
    with pytest.raises(SpecValidationError, match="not up-hierarchy"):
        corpus.validate()


def test_two_documents_defining_one_policy_is_refused() -> None:
    """Inline authority is sugar for an actor-scoped policy, not an override of one.
    Two sources for one row is how a policy comes to depend on file ordering."""
    corpus = valid()
    corpus.edit("Actor", "marketing-head", authority={"allowed": ["research"]})
    corpus.add(
        "AuthorityPolicy",
        "actor-head-research",
        {"scope": "actor", "target": "marketing-head", "action": "research", "level": "denied"},
    )
    with pytest.raises(SpecValidationError, match="two documents define authority"):
        corpus.validate()


# --- delegation (v3 §10) -------------------------------------------------------------------------


def test_delegation_disabled_is_not_checked() -> None:
    valid().validate()


def test_delegation_enabled_with_zero_limits_is_refused() -> None:
    corpus = valid().edit(
        "Actor",
        "marketing-head",
        delegation={"enabled": True, "maxDepth": 0, "maxChildren": 3, "maxLiveDescendants": 6},
    )
    with pytest.raises(SpecValidationError, match="never delegate"):
        corpus.validate()


def test_a_child_may_not_hold_a_tool_its_delegating_parent_lacks() -> None:
    corpus = valid().edit(
        "Actor",
        "marketing-head",
        delegation={"enabled": True, "maxDepth": 2, "maxChildren": 3, "maxLiveDescendants": 6},
    )
    with pytest.raises(SpecValidationError, match="Delegation cannot widen"):
        corpus.validate()


def test_a_child_whose_tools_are_a_subset_is_allowed() -> None:
    corpus = valid()
    corpus.edit(
        "Actor",
        "marketing-head",
        tools=["publish.external@1", "web.search@1", "web.fetch@1"],
        delegation={"enabled": True, "maxDepth": 2, "maxChildren": 3, "maxLiveDescendants": 6},
    )
    corpus.add(
        "ToolGrant",
        "actor-head-search",
        {"subject": {"actor": "marketing-head"}, "tool": "web.search@1"},
    )
    corpus.add(
        "ToolGrant",
        "actor-head-fetch",
        {"subject": {"actor": "marketing-head"}, "tool": "web.fetch@1"},
    )
    corpus.validate()


# --- budget --------------------------------------------------------------------------------------


def test_department_limits_may_not_exceed_the_organization() -> None:
    """Catches an impossible org before it half-runs."""
    with pytest.raises(SpecValidationError, match="organization would run out"):
        valid().edit(
            "BudgetPolicy", "default", organization=10_000, departments={"marketing": 60_000}
        ).validate()


def test_actor_limits_may_not_exceed_their_department() -> None:
    with pytest.raises(SpecValidationError, match="actor limits in department marketing"):
        valid().edit(
            "BudgetPolicy",
            "default",
            organization=100_000,
            departments={"marketing": 10_000},
            actors={"research": 20_000},
        ).validate()


def test_oversubscription_is_allowed_up_to_k() -> None:
    """v3 §8's soft ceiling. Children may sum past the parent because allocations are
    advisory and the hard reservation path is still bounded per level."""
    valid().edit(
        "BudgetPolicy",
        "default",
        oversubscription=0.3,
        organization=100_000,
        departments={"marketing": 60_000},
        actors={"research": 40_000, "content": 38_000},
    ).validate()


def test_a_budget_naming_an_unknown_actor_is_refused() -> None:
    with pytest.raises(SpecValidationError, match=r"actors\.ghost has no Actor document"):
        valid().edit("BudgetPolicy", "default", actors={"ghost": 100}).validate()


def test_two_budget_policies_are_refused() -> None:
    with pytest.raises(SpecValidationError, match="more than one BudgetPolicy"):
        valid().add("BudgetPolicy", "second", {"organization": 1000}).validate()


# --- triggers ------------------------------------------------------------------------------------


def test_a_trigger_for_an_unknown_actor_is_refused() -> None:
    with pytest.raises(SpecValidationError, match="is not an Actor"):
        valid().edit("Trigger", "weekly-plan", actor="ghost").validate()


def test_a_malformed_cron_is_refused_before_anything_is_written() -> None:
    with pytest.raises(SpecValidationError, match="does not parse"):
        valid().edit("Trigger", "weekly-plan", cron="not a cron").validate()


def test_a_triggers_payload_cannot_contradict_its_own_key() -> None:
    org = (
        valid()
        .edit("Trigger", "weekly-plan", input={"mode": "lies", "trigger": "lies", "extra": 1})
        .compile()
    )
    trigger = next(t for t in org.triggers if t.key == "weekly-plan")
    assert trigger.input == {"mode": "weekly_plan", "trigger": "weekly-plan", "extra": 1}


# --- the dependency graph ------------------------------------------------------------------------


def test_the_department_head_cycle_is_broken_not_reported() -> None:
    """§7. A department names its head and the head names the department; that is
    structural, and the edge is deferred rather than refused."""
    org = valid().compile()
    assert ("Department/marketing", "head", "Actor/marketing-head") in org.deferred
    assert ("Actor/marketing-head", "department", "Department/marketing") in org.deferred
    assert org.order  # it sorted anyway


def test_a_department_parent_cycle_is_refused_with_a_useful_message() -> None:
    """`parent` is a phase-2 reference column, so the cycle reaches `validation` — which
    names the two departments — rather than coming out of the sort as a list of every
    document that transitively depended on them."""
    corpus = valid()
    corpus.add("Department", "alpha", {"parent": "beta", "head": None, "description": ""})
    corpus.add("Department", "beta", {"parent": "alpha", "head": None, "description": ""})
    with pytest.raises(SpecValidationError, match="department tree contains a cycle"):
        corpus.validate()


def test_topological_order_is_canonical() -> None:
    """Two loads of the same documents produce the same order, therefore the same plan,
    therefore the same plan_hash (edge case 76 depends on it)."""
    assert valid().compile().order == valid().compile().order


def test_a_dependency_precedes_its_dependent() -> None:
    """Only the edges phase 1 needs: a grant after its subject and its connection, an
    actor after the model profiles its spec resolves."""
    order = [f"{kind.value}/{name}" for kind, name in valid().compile().order]
    assert order.index("ModelProfile/deepseek-v4-pro-search-nothink") < order.index(
        "Actor/research"
    )
    assert order.index("Actor/research") < order.index("ToolGrant/actor-research-web-search-1")
    assert order.index("Connection/search-primary") < order.index(
        "ToolGrant/actor-research-web-search-1"
    )
    assert order.index("Actor/marketing-head") < order.index("Trigger/weekly-plan")
