"""Read-only observation surface.

The four endpoints in `app.py` are the control surface: they start a run and
follow it. Nothing there answers "what organizations exist, who is in them, and
what are they doing right now" — which is the only question a viewer asks.

Three rules hold this module together:

1. **Read only.** Every statement here is a SELECT. Nothing in this file may
   start, cancel or mutate anything; a viewer that can change the org is a
   control surface wearing an observability costume, and it would need the
   admission, authority and budget checks `RunService` owns.
2. **The organization is in the path, not in a header.** `X-Organization-Id` is
   right for the control surface, where every call is already scoped to one
   tenant. A viewer's first question is *which* tenant, so it cannot supply the
   answer as a precondition. Path scoping keeps every query just as scoped.
3. **No budget pool reads.** Per-actor spend is summed from `usage_ledger`, the
   append-only record, never from `budget_pools` — those tables belong to
   `runtime.budget` and the import-linter contract says so.

The graph endpoint denormalises hard on purpose. A node-based viewer needs the
whole org in one paint, and eight round trips to assemble one canvas is how a
viewer ends up showing four different instants at once.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from runtime.persistence.uow import UnitOfWorkFactory

router = APIRouter(prefix="/v1/observe", tags=["observe"])

TERMINAL = ("SUCCESS", "FAILED", "CANCELLED", "ABANDONED")
LIVE = ("QUEUED", "RUNNING")
OPEN_TASK = ("DRAFT", "ASSIGNED", "IN_PROGRESS", "SUBMITTED")
HEARTBEAT_S = 15.0


def _uow(request: Request) -> UnitOfWorkFactory:
    factory: UnitOfWorkFactory = request.app.state.uow
    return factory


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _fetch(request: Request, sql: str, params: dict[str, Any]) -> Sequence[Any]:
    async with _uow(request)() as uow:
        return (await uow.session.execute(text(sql), params)).all()


# --- organizations -----------------------------------------------------------------

ORGS_SQL = """
SELECT o.id,
       o.name,
       o.created_at,
       (SELECT count(*) FROM departments d
         WHERE d.organization_id = o.id AND d.active) AS departments,
       (SELECT count(*) FROM actors a
         WHERE a.organization_id = o.id AND a.active) AS actors,
       (SELECT count(*) FROM runs r
         WHERE r.organization_id = o.id AND r.status = ANY(:live)) AS runs_active,
       (SELECT count(*) FROM runs r WHERE r.organization_id = o.id) AS runs_total,
       (SELECT count(*) FROM runs r
         WHERE r.organization_id = o.id AND r.status = 'FAILED') AS runs_failed,
       (SELECT count(*) FROM tasks t
         WHERE t.organization_id = o.id AND t.status = ANY(:open)) AS tasks_open,
       (SELECT count(*) FROM goals g
         WHERE g.organization_id = o.id AND g.active) AS goals,
       (SELECT count(*) FROM kill_switches k
         WHERE k.organization_id = o.id AND k.disengaged_at IS NULL) AS kill_switches,
       (SELECT max(coalesce(r.ended_at, r.started_at, r.created_at)) FROM runs r
         WHERE r.organization_id = o.id) AS last_activity_at
  FROM organizations o
 ORDER BY o.name
"""


def _status(runs_active: int, kill_switches: int) -> str:
    """A halted org outranks a busy one.

    An engaged kill switch means the runs still counted as live are the ones
    draining, not the ones working. Showing that as "running" is the single most
    misleading thing this endpoint could say.
    """
    if kill_switches:
        return "halted"
    return "running" if runs_active else "idle"


@router.get("/organizations")
async def list_organizations(request: Request) -> dict[str, Any]:
    rows = await _fetch(request, ORGS_SQL, {"live": list(LIVE), "open": list(OPEN_TASK)})
    return {
        "organizations": [
            {
                "id": str(r.id),
                "name": r.name,
                "created_at": _iso(r.created_at),
                "status": _status(r.runs_active, r.kill_switches),
                "departments": r.departments,
                "actors": r.actors,
                "runs_active": r.runs_active,
                "runs_total": r.runs_total,
                "runs_failed": r.runs_failed,
                "tasks_open": r.tasks_open,
                "goals": r.goals,
                "kill_switches": r.kill_switches,
                "last_activity_at": _iso(r.last_activity_at),
            }
            for r in rows
        ]
    }


# --- graph -------------------------------------------------------------------------

DEPARTMENTS_SQL = """
SELECT id, name, parent_id, head_actor_name, description
  FROM departments
 WHERE organization_id = :org AND active
 ORDER BY name
"""

ACTORS_SQL = """
SELECT a.id,
       a.name,
       a.kind,
       a.role_name,
       a.department,
       a.department_id,
       a.reports_to,
       a.active,
       a.created_at,
       av.version,
       av.spec,
       av.spec_hash,
       r.rank
  FROM actors a
  LEFT JOIN actor_versions av ON av.id = a.active_version_id
  LEFT JOIN roles r
         ON r.organization_id = a.organization_id AND r.name = a.role_name
 WHERE a.organization_id = :org
 ORDER BY coalesce(r.rank, 9999), a.name
"""

RUN_STATS_SQL = """
SELECT actor_id,
       count(*) FILTER (WHERE status = ANY(:live)) AS active,
       count(*) FILTER (WHERE status = 'RUNNING') AS running,
       count(*) FILTER (WHERE status = 'SUCCESS') AS success,
       count(*) FILTER (WHERE status = 'FAILED') AS failed,
       count(*) AS total,
       max(coalesce(ended_at, started_at, created_at)) AS last_run_at
  FROM runs
 WHERE organization_id = :org
 GROUP BY actor_id
"""

SPEND_SQL = """
SELECT r.actor_id, coalesce(sum(u.cost_cents), 0)::bigint AS cents
  FROM usage_ledger u
  JOIN runs r ON r.id = u.run_id
 WHERE u.organization_id = :org
 GROUP BY r.actor_id
"""

TASK_STATS_SQL = """
SELECT assignee_name,
       count(*) FILTER (WHERE status = ANY(:open)) AS open,
       count(*) FILTER (WHERE outcome IN ('ACCEPTED', 'ACCEPTED_WITH_EDITS',
                                          'AUTO_ACCEPTED')) AS accepted,
       count(*) FILTER (WHERE outcome IN ('REJECTED', 'REWORK_REQUIRED')) AS rejected,
       count(*) AS total
  FROM tasks
 WHERE organization_id = :org AND assignee_name IS NOT NULL
 GROUP BY assignee_name
"""

GOALS_SQL = """
SELECT id, name, statement, horizon, active
  FROM goals WHERE organization_id = :org ORDER BY active DESC, name
"""

PROJECTS_SQL = """
SELECT id, name, description, goal_id, owner_actor_id, active
  FROM projects WHERE organization_id = :org ORDER BY active DESC, name
"""

TRIGGERS_SQL = """
SELECT key, actor_name, cron, timezone, active, last_evaluated_at
  FROM triggers WHERE organization_id = :org ORDER BY key
"""

KILL_SWITCHES_SQL = """
SELECT scope_type, scope_id, mode, reason, engaged_by, engaged_at
  FROM kill_switches
 WHERE organization_id = :org AND disengaged_at IS NULL
"""

DELEGATION_EDGES_SQL = """
SELECT pa.name AS parent_name,
       d.target_actor AS child_name,
       count(*) AS calls,
       count(*) FILTER (WHERE d.status = 'REFUSED') AS refused
  FROM delegations d
  JOIN runs pr ON pr.id = d.parent_run_id
  JOIN actors pa ON pa.id = pr.actor_id
 WHERE d.organization_id = :org
 GROUP BY 1, 2
"""


def _ceilings(spec: dict[str, Any] | None) -> dict[str, Any]:
    return (spec or {}).get("ceilings") or {}


def _modes(spec: dict[str, Any] | None) -> list[str]:
    """The entry points this actor's graph or handler routes on.

    Read from the registries, which is a fact about *this process* — so an API that has
    not imported the graph packages reports `[]`, and a caller offering a choice falls
    back to free text rather than to a wrong list. `app.py` imports them at startup for
    exactly this reason.

    Deliberately not `runtime.org.department.MODES`: that set is derived from the M1
    schedule, so it names `weekly_metrics` for a graph that does not dispatch on it and
    omits `task.submitted` for one that does.
    """
    from runtime.graphs.registry import modes_for as graph_modes
    from runtime.handlers.registry import modes_for as handler_modes

    body = spec or {}
    graph_ref, handler_ref = body.get("graph_ref"), body.get("handler_ref")
    if graph_ref:
        return list(graph_modes(str(graph_ref)))
    if handler_ref:
        return list(handler_modes(str(handler_ref)))
    return []


def _department_state(
    *,
    stopped: bool,
    has_active_trigger: bool,
    runs_active: int,
) -> str:
    """Derived, never stored.

    A stored state would be a second answer to a question the kill switches and the
    triggers already answer between them, and the two would disagree the first time
    somebody used the CLI. Precedence: a stop outranks everything, then anything that
    will produce work, then nothing.
    """
    if stopped:
        return "stopped"
    if has_active_trigger or runs_active:
        return "running"
    return "paused"


def _profiles(spec: dict[str, Any] | None) -> dict[str, str]:
    """Flatten the model profile block to `call site -> model`.

    The stored shape is `{"profiles": {site: {model, provider, ...}}}`. A viewer
    wants to see which model answers for which call site; the rest of the entry
    is per-call configuration nobody reads off a node.
    """
    profiles = ((spec or {}).get("model_profiles") or {}).get("profiles") or {}
    out: dict[str, str] = {}
    for site, entry in profiles.items():
        if isinstance(entry, dict):
            model = entry.get("model")
            provider = entry.get("provider")
            out[site] = f"{model}" + (f" ({provider})" if provider else "")
    return out


@router.get("/organizations/{org_id}/graph")
async def organization_graph(org_id: UUID, request: Request) -> dict[str, Any]:
    """The whole organization as nodes and edges, read in one transaction.

    One transaction rather than one per query: the department list and the run
    counts painted next to it should be the same instant, or the canvas shows a
    department that has an actor the actor list does not.
    """
    async with _uow(request)() as uow:
        session = uow.session
        org_row = (
            await session.execute(
                text("SELECT id, name, created_at FROM organizations WHERE id = :org"),
                {"org": org_id},
            )
        ).first()
        if org_row is None:
            raise HTTPException(status_code=404, detail=f"no organization {org_id}")

        p = {"org": org_id}
        departments = (await session.execute(text(DEPARTMENTS_SQL), p)).all()
        actors = (await session.execute(text(ACTORS_SQL), p)).all()
        run_stats = (await session.execute(text(RUN_STATS_SQL), {**p, "live": list(LIVE)})).all()
        spend = (await session.execute(text(SPEND_SQL), p)).all()
        task_stats = (
            await session.execute(text(TASK_STATS_SQL), {**p, "open": list(OPEN_TASK)})
        ).all()
        goals = (await session.execute(text(GOALS_SQL), p)).all()
        projects = (await session.execute(text(PROJECTS_SQL), p)).all()
        triggers = (await session.execute(text(TRIGGERS_SQL), p)).all()
        delegations = (await session.execute(text(DELEGATION_EDGES_SQL), p)).all()
        kill_switches = (await session.execute(text(KILL_SWITCHES_SQL), p)).all()

    runs_by_actor = {r.actor_id: r for r in run_stats}
    spend_by_actor = {r.actor_id: int(r.cents) for r in spend}
    tasks_by_name = {r.assignee_name: r for r in task_stats}
    triggers_by_actor: dict[str, list[dict[str, Any]]] = {}
    for t in triggers:
        triggers_by_actor.setdefault(t.actor_name, []).append(
            {
                "key": t.key,
                # The actor is on the row even though this index is keyed by it: a
                # department node shows its members' triggers pooled together, and a
                # list of crons with no owner is a list nobody can act on.
                "actor": t.actor_name,
                "cron": t.cron,
                "timezone": t.timezone,
                "active": t.active,
                "last_evaluated_at": _iso(t.last_evaluated_at),
            }
        )

    org_node_id = f"org:{org_row.id}"
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    live_total = sum(int(r.active) for r in run_stats)
    nodes.append(
        {
            "id": org_node_id,
            "type": "organization",
            "label": org_row.name,
            "data": {
                "id": str(org_row.id),
                "created_at": _iso(org_row.created_at),
                "departments": len(departments),
                "actors": len(actors),
                "runs_active": live_total,
                "spend_cents": sum(spend_by_actor.values()),
                "kill_switches": [
                    {
                        "scope_type": k.scope_type,
                        "scope_id": str(k.scope_id) if k.scope_id else None,
                        "mode": k.mode,
                        "reason": k.reason,
                        "engaged_by": k.engaged_by,
                        "engaged_at": _iso(k.engaged_at),
                    }
                    for k in kill_switches
                ],
            },
        }
    )

    dept_node = {d.id: f"dept:{d.id}" for d in departments}
    for d in departments:
        members = [a for a in actors if a.department_id == d.id or a.department == d.name]
        member_triggers = [t for a in members for t in triggers_by_actor.get(a.name, [])]
        # An org switch covers every department; a department switch covers the one it
        # names. Both are reported as *this department's* stop, because from the
        # canvas's point of view that is what they are.
        covering = [
            k
            for k in kill_switches
            if k.scope_type == "org" or (k.scope_type == "department" and k.scope_id == d.name)
        ]
        runs_active = sum(
            int(runs_by_actor[a.id].active) for a in members if a.id in runs_by_actor
        )
        nodes.append(
            {
                "id": dept_node[d.id],
                "type": "department",
                "label": d.name,
                "data": {
                    "id": str(d.id),
                    "description": d.description or "",
                    "head": d.head_actor_name,
                    "members": len(members),
                    "runs_active": runs_active,
                    "state": _department_state(
                        stopped=bool(covering),
                        has_active_trigger=any(t["active"] for t in member_triggers),
                        runs_active=runs_active,
                    ),
                    "triggers": member_triggers,
                    "kill_switch": (
                        {
                            "scope_type": covering[0].scope_type,
                            "scope_id": covering[0].scope_id,
                            "mode": covering[0].mode,
                            "reason": covering[0].reason,
                            "engaged_by": covering[0].engaged_by,
                            "engaged_at": _iso(covering[0].engaged_at),
                        }
                        if covering
                        else None
                    ),
                },
            }
        )
        parent = dept_node.get(d.parent_id) if d.parent_id else None
        edges.append(
            {
                "id": f"contains:{parent or org_node_id}->{dept_node[d.id]}",
                "source": parent or org_node_id,
                "target": dept_node[d.id],
                "kind": "contains",
            }
        )

    actor_node = {a.name: f"actor:{a.id}" for a in actors}
    for a in actors:
        stats = runs_by_actor.get(a.id)
        tasks = tasks_by_name.get(a.name)
        nodes.append(
            {
                "id": actor_node[a.name],
                "type": "actor",
                "label": a.name,
                "data": {
                    "id": str(a.id),
                    "kind": a.kind,
                    "role": a.role_name,
                    "rank": a.rank,
                    "department": a.department,
                    "reports_to": a.reports_to,
                    "active": bool(a.active),
                    "version": a.version,
                    "spec_hash": a.spec_hash,
                    "graph_ref": (a.spec or {}).get("graph_ref"),
                    "handler_ref": (a.spec or {}).get("handler_ref"),
                    # What `input.mode` may be, so a caller can offer the choices
                    # instead of asking somebody to remember them. `[]` means the
                    # entrypoint declared none — fall back to free text.
                    "modes": _modes(a.spec),
                    "tools": (a.spec or {}).get("allowed_tools") or [],
                    "model_profiles": _profiles(a.spec),
                    "ceilings": _ceilings(a.spec),
                    "triggers": triggers_by_actor.get(a.name, []),
                    "spend_cents": spend_by_actor.get(a.id, 0),
                    "runs": {
                        "active": int(stats.active) if stats else 0,
                        "running": int(stats.running) if stats else 0,
                        "success": int(stats.success) if stats else 0,
                        "failed": int(stats.failed) if stats else 0,
                        "total": int(stats.total) if stats else 0,
                    },
                    "last_run_at": _iso(stats.last_run_at) if stats else None,
                    "tasks": {
                        "open": int(tasks.open) if tasks else 0,
                        "accepted": int(tasks.accepted) if tasks else 0,
                        "rejected": int(tasks.rejected) if tasks else 0,
                        "total": int(tasks.total) if tasks else 0,
                    },
                    "status": _actor_status(stats),
                },
            }
        )

    for a in actors:
        target = actor_node[a.name]
        if a.reports_to and a.reports_to in actor_node:
            edges.append(
                {
                    "id": f"reports:{target}",
                    "source": actor_node[a.reports_to],
                    "target": target,
                    "kind": "reports_to",
                }
            )
            continue
        # No manager. It hangs off its department if it has one, off the org if not
        # — an actor floating unattached reads as a rendering bug rather than as the
        # unplaced actor it is.
        anchor = None
        if a.department_id and a.department_id in dept_node:
            anchor = dept_node[a.department_id]
        else:
            anchor = next(
                (dept_node[d.id] for d in departments if d.name == a.department),
                org_node_id,
            )
        edges.append(
            {
                "id": f"member:{anchor}->{target}",
                "source": anchor,
                "target": target,
                "kind": "heads" if a.name in {d.head_actor_name for d in departments} else "member",
            }
        )

    for g in goals:
        gid = f"goal:{g.id}"
        nodes.append(
            {
                "id": gid,
                "type": "goal",
                "label": g.name,
                "data": {
                    "id": str(g.id),
                    "statement": g.statement,
                    "horizon": g.horizon,
                    "active": bool(g.active),
                },
            }
        )
        edges.append(
            {
                "id": f"pursues:{gid}",
                "source": org_node_id,
                "target": gid,
                "kind": "pursues",
            }
        )

    actor_by_id = {a.id: a for a in actors}
    for pr in projects:
        pid = f"project:{pr.id}"
        owner = actor_by_id.get(pr.owner_actor_id) if pr.owner_actor_id else None
        nodes.append(
            {
                "id": pid,
                "type": "project",
                "label": pr.name,
                "data": {
                    "id": str(pr.id),
                    "description": pr.description or "",
                    "active": bool(pr.active),
                    "owner": owner.name if owner else None,
                },
            }
        )
        edges.append(
            {
                "id": f"project:{pid}",
                "source": f"goal:{pr.goal_id}",
                "target": pid,
                "kind": "project",
            }
        )
        if owner is not None:
            edges.append(
                {
                    "id": f"owns:{pid}->{actor_node[owner.name]}",
                    "source": pid,
                    "target": actor_node[owner.name],
                    "kind": "owns",
                }
            )

    for d in delegations:
        if d.parent_name in actor_node and d.child_name in actor_node:
            edges.append(
                {
                    "id": f"delegates:{d.parent_name}->{d.child_name}",
                    "source": actor_node[d.parent_name],
                    "target": actor_node[d.child_name],
                    "kind": "delegates",
                    "data": {"calls": int(d.calls), "refused": int(d.refused)},
                }
            )

    return {
        "organization": {
            "id": str(org_row.id),
            "name": org_row.name,
            "created_at": _iso(org_row.created_at),
            "status": _status(live_total, len(kill_switches)),
        },
        "nodes": nodes,
        "edges": edges,
    }


def _actor_status(stats: Any) -> str:
    if stats is None or int(stats.total) == 0:
        return "idle"
    if int(stats.running):
        return "running"
    if int(stats.active):
        return "queued"
    return "idle"


# --- activity ----------------------------------------------------------------------

RUNS_SQL = """
SELECT r.id,
       r.status,
       r.status_reason,
       r.created_at,
       r.started_at,
       r.ended_at,
       r.parent_run_id,
       r.root_run_id,
       r.task_id,
       r.priority,
       r.depth,
       a.name AS actor_name,
       a.department,
       coalesce((SELECT sum(u.cost_cents) FROM usage_ledger u WHERE u.run_id = r.id), 0)
         AS cost_cents
  FROM runs r
  JOIN actors a ON a.id = r.actor_id
 WHERE r.organization_id = :org
   -- CAST rather than `:actor::text`: SQLAlchemy's `text()` refuses to bind a
   -- parameter followed by a colon, so the Postgres cast shorthand would ship
   -- the placeholder to the server verbatim.
   AND (CAST(:actor AS text) IS NULL OR a.name = CAST(:actor AS text))
 ORDER BY r.created_at DESC
 LIMIT :limit
"""

CHILD_RUNS_SQL = """
SELECT r.id,
       r.status,
       r.status_reason,
       r.created_at,
       r.started_at,
       r.ended_at,
       r.parent_run_id,
       r.root_run_id,
       r.task_id,
       r.priority,
       r.depth,
       a.name AS actor_name,
       a.department,
       coalesce((SELECT sum(u.cost_cents) FROM usage_ledger u WHERE u.run_id = r.id), 0)
         AS cost_cents
  FROM runs r
  JOIN actors a ON a.id = r.actor_id
 WHERE r.parent_run_id = :parent
 ORDER BY r.created_at
 LIMIT 50
"""

TASKS_SQL = """
SELECT id, title, objective, status, outcome, outcome_reason, assignee_name,
       rework_count, created_at, submitted_at, closed_at, due_at
  FROM tasks
 WHERE organization_id = :org
 ORDER BY created_at DESC
 LIMIT :limit
"""

EVENTS_SQL = """
SELECT e.id, e.topic, e.payload, e.created_at, e.run_id, a.name AS actor_name
  FROM events e
  LEFT JOIN runs r ON r.id = e.run_id
  LEFT JOIN actors a ON a.id = r.actor_id
 WHERE e.organization_id = :org AND e.id > :after
 ORDER BY e.id DESC
 LIMIT :limit
"""


def _run_row(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "actor": r.actor_name,
        "department": r.department,
        "status": r.status,
        "status_reason": r.status_reason,
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        "root_run_id": str(r.root_run_id),
        "task_id": str(r.task_id) if r.task_id else None,
        "priority": r.priority,
        "depth": r.depth,
        "cost_cents": int(r.cost_cents or 0),
        "created_at": _iso(r.created_at),
        "started_at": _iso(r.started_at),
        "ended_at": _iso(r.ended_at),
    }


@router.get("/organizations/{org_id}/activity")
async def organization_activity(
    org_id: UUID,
    request: Request,
    limit: int = Query(default=40, ge=1, le=200),
    actor: str | None = Query(default=None),
) -> dict[str, Any]:
    async with _uow(request)() as uow:
        session = uow.session
        runs = (
            await session.execute(text(RUNS_SQL), {"org": org_id, "limit": limit, "actor": actor})
        ).all()
        tasks = (await session.execute(text(TASKS_SQL), {"org": org_id, "limit": limit})).all()
        events = (
            await session.execute(text(EVENTS_SQL), {"org": org_id, "limit": limit, "after": 0})
        ).all()

    return {
        "runs": [_run_row(r) for r in runs],
        "tasks": [
            {
                "id": str(t.id),
                "title": t.title,
                "objective": t.objective,
                "status": t.status,
                "outcome": t.outcome,
                "outcome_reason": t.outcome_reason,
                "assignee": t.assignee_name,
                "rework_count": t.rework_count,
                "created_at": _iso(t.created_at),
                "submitted_at": _iso(t.submitted_at),
                "closed_at": _iso(t.closed_at),
                "due_at": _iso(t.due_at),
            }
            for t in tasks
        ],
        "events": [
            {
                "id": int(e.id),
                "topic": e.topic,
                "payload": e.payload,
                "run_id": str(e.run_id) if e.run_id else None,
                "actor": e.actor_name,
                "created_at": _iso(e.created_at),
            }
            for e in events
        ],
    }


# --- one actor, one run ------------------------------------------------------------


@router.get("/actors/{actor_id}")
async def actor_detail(actor_id: UUID, request: Request) -> dict[str, Any]:
    async with _uow(request)() as uow:
        session = uow.session
        row = (
            await session.execute(
                text(
                    """
                    SELECT a.id, a.organization_id, a.name, a.kind, a.role_name,
                           a.department, a.reports_to, a.active, a.created_at,
                           av.version, av.spec, av.spec_hash
                      FROM actors a
                      LEFT JOIN actor_versions av ON av.id = a.active_version_id
                     WHERE a.id = :id
                    """
                ),
                {"id": actor_id},
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no actor {actor_id}")
        versions = (
            await session.execute(
                text(
                    """
                    SELECT version, spec_hash, created_at FROM actor_versions
                     WHERE actor_id = :id ORDER BY version DESC LIMIT 20
                    """
                ),
                {"id": actor_id},
            )
        ).all()
        runs = (
            await session.execute(
                text(RUNS_SQL),
                {"org": row.organization_id, "limit": 25, "actor": row.name},
            )
        ).all()
        reports = (
            await session.execute(
                text("SELECT id, name FROM actors WHERE reports_to = :name"),
                {"name": row.name},
            )
        ).all()

    return {
        "id": str(row.id),
        "organization_id": str(row.organization_id),
        "name": row.name,
        "kind": row.kind,
        "role": row.role_name,
        "department": row.department,
        "reports_to": row.reports_to,
        "reports": [{"id": str(r.id), "name": r.name} for r in reports],
        "active": bool(row.active),
        "created_at": _iso(row.created_at),
        "version": row.version,
        "spec_hash": row.spec_hash,
        "spec": row.spec,
        "versions": [
            {"version": v.version, "spec_hash": v.spec_hash, "created_at": _iso(v.created_at)}
            for v in versions
        ],
        "runs": [_run_row(r) for r in runs],
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: UUID, request: Request) -> dict[str, Any]:
    """The same run view `app.py` serves, minus the header and plus its events.

    A viewer that has just clicked a node on a canvas has an organization from
    the canvas, not from a header it was asked to set — and it wants the event
    log in the same paint as the status.
    """
    async with _uow(request)() as uow:
        session = uow.session
        row = (
            await session.execute(
                text(
                    """
                    SELECT r.id, r.organization_id, r.status, r.status_reason, r.created_at,
                           r.started_at, r.ended_at, r.parent_run_id, r.root_run_id, r.task_id,
                           r.priority, r.depth, r.fence, r.lease_expiries, r.thread_id,
                           a.name AS actor_name, a.department,
                           coalesce((SELECT sum(u.cost_cents) FROM usage_ledger u
                                      WHERE u.run_id = r.id), 0) AS cost_cents
                      FROM runs r JOIN actors a ON a.id = r.actor_id
                     WHERE r.id = :id
                    """
                ),
                {"id": run_id},
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        events = await uow.outbox.events_for_run(run_id)
        effects = await uow.effects.for_run(run_id)
        artifacts = await uow.artifacts.for_run(run_id)
        children = (await session.execute(text(CHILD_RUNS_SQL), {"parent": run_id})).all()

    output: dict[str, Any] | None = None
    for event in reversed(events):
        if event["topic"] in ("run.succeeded", "run.failed"):
            output = event["payload"].get("output")
            break

    return {
        **_run_row(row),
        "organization_id": str(row.organization_id),
        "fence": row.fence,
        "lease_expiries": row.lease_expiries,
        "thread_id": row.thread_id,
        "output": output,
        "events": events,
        "children": [_run_row(c) for c in children],
        "effects": [
            {
                "logical_call_id": e.logical_call_id,
                "tool": f"{e.tool_name}@{e.tool_version}",
                "status": e.status.value,
                "attempts": e.attempts,
                "recovery_policy": e.recovery_policy,
            }
            for e in effects
        ],
        "artifacts": [
            {
                "artifact_id": str(a.artifact_id),
                "version": a.version,
                "uri": a.uri,
                "size_bytes": a.size_bytes,
                "sha256": a.sha256,
                "content_type": a.content_type,
            }
            for a in artifacts
        ],
    }


# --- live tail ---------------------------------------------------------------------


@router.get("/organizations/{org_id}/stream")
async def organization_stream(
    org_id: UUID,
    request: Request,
    after: int = Query(default=0, ge=0, description="Last event id already seen"),
) -> StreamingResponse:
    """Org-wide SSE tail, backed by `events` for the same reason the run tail is.

    `after` is an event id, so a viewer that reconnects resumes exactly where it
    stopped instead of replaying the org's whole history onto the canvas.
    """
    factory = _uow(request)

    async def _events() -> AsyncIterator[str]:
        # An SSE comment, sent before anything is known. Response headers are not
        # necessarily flushed until the first byte of the body arrives, and a quiet
        # organization produces no first byte — so without this a browser's
        # EventSource stays in CONNECTING and the viewer reports the tail as broken
        # when it is merely idle. Comments are ignored by every SSE client.
        yield ": open\n\n"

        loop = asyncio.get_running_loop()
        last_beat = loop.time()
        cursor = after
        idle = 0.1
        while True:
            if await request.is_disconnected():
                return
            async with factory() as uow:
                batch = (
                    await uow.session.execute(
                        text(
                            """
                            SELECT e.id, e.topic, e.payload, e.created_at, e.run_id,
                                   a.name AS actor_name
                              FROM events e
                              LEFT JOIN runs r ON r.id = e.run_id
                              LEFT JOIN actors a ON a.id = r.actor_id
                             WHERE e.organization_id = :org AND e.id > :after
                             ORDER BY e.id
                             LIMIT 200
                            """
                        ),
                        {"org": org_id, "after": cursor},
                    )
                ).all()

            for row in batch:
                cursor = int(row.id)
                # The default SSE event name, not the topic. The run tail names the
                # topic because a client there follows one run and knows its
                # vocabulary; an org tail carries every topic the runtime has, and a
                # browser's EventSource can only listen for names it was told in
                # advance — so a new topic would be silently invisible. The topic is
                # in the payload instead, where an unknown one still arrives.
                yield _sse(
                    "message",
                    {
                        "id": cursor,
                        "topic": row.topic,
                        "payload": row.payload,
                        "run_id": str(row.run_id) if row.run_id else None,
                        "actor": row.actor_name,
                        "created_at": _iso(row.created_at),
                    },
                    event_id=cursor,
                )

            now = loop.time()
            if batch:
                last_beat = now
            elif now - last_beat > HEARTBEAT_S:
                # Keeps an idle connection alive through whatever proxy is in front
                # of it, and lets the client tell "quiet" from "dropped".
                last_beat = now
                yield ": beat\n\n"

            idle = 0.1 if batch else min(idle * 2, 2.0)
            await asyncio.sleep(idle)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"
