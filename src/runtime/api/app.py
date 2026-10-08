"""FastAPI application.

Four endpoints here, plus two mounted routers, and one rule that holds across all of
them: the API never executes a run. `POST /v1/runs` resolves, admits, writes and
returns in about 15ms; a worker picks the run up from the stream. Executing on the
request path would put graph latency in front of the caller and, worse, would give the
caller's connection ownership of a run that nobody else could recover if it dropped.

The two routers are deliberately separate modules. `/v1/observe` is read-only and says
so in its first line; `/v1/control` writes, and every write it makes goes through the
same `RunService`, the same kill switch and the same spec compiler the CLI uses. Two
files rather than one so that "this module contains only SELECTs" stays a fact about
`observe.py` rather than a convention somebody has to maintain.

The SSE tail reads from the `events` table, not from Redis. A reconnecting client
gets a complete ordered history from any point, and a Redis wipe costs it nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from runtime.api.attachments import router as attachments_router
from runtime.api.bots import computer_router as bots_computer_router
from runtime.api.bots import router as bots_router
from runtime.api.bots import vault_router as bots_vault_router
from runtime.api.connectors import marketplace_router as connectors_market_router
from runtime.api.connectors import router as connectors_router
from runtime.api.control import router as control_router
from runtime.api.errors import http_errors
from runtime.api.groups import reactions_router
from runtime.api.groups import router as groups_router
from runtime.api.observe import router as observe_router
from runtime.api.routines import hooks_router
from runtime.api.routines import router as routines_router
from runtime.api.schemas import (
    ArtifactView,
    EffectView,
    HealthResponse,
    RunView,
    StartRunBody,
    StartRunResponse,
)
from runtime.api.skills import marketplace_router, teach_router
from runtime.api.skills import router as skills_router
from runtime.api.workspace import router as workspace_router
from runtime.domain.ids import OrganizationId, RunId, SessionId
from runtime.domain.specs import StartRunRequest
from runtime.events.stream import RedisStreams
from runtime.observability.logging import configure_logging, get_logger
from runtime.observability.tracing import configure_tracing
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.engine import dispose_engines
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.settings import Settings, get_settings

log = get_logger("api")

ORG_HEADER = "X-Organization-Id"
"""M0 has no auth and no multi-tenancy. The org comes from a header so that every
query is already scoped by it — retrofitting the scope later is the expensive
version of this decision."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    configure_tracing(service_name=settings.service_name, enabled=settings.otel_enabled)
    app.state.settings = settings
    app.state.uow = UnitOfWorkFactory(settings)
    app.state.streams = RedisStreams(settings)
    # One `KillSwitchService` for the process, handed to `RunService` and reached by
    # `/v1/control` through `RunService.kill_switches`. Two instances would each cache
    # their own copy of the switch list, and the endpoint that engages a switch would
    # invalidate a cache the endpoint that admits runs never reads.
    app.state.kill_switches = KillSwitchService(app.state.uow)
    app.state.service = RunService(
        app.state.uow, settings=settings, kill_switches=app.state.kill_switches
    )
    try:
        yield
    finally:
        await app.state.streams.close()
        await dispose_engines()


def create_app(settings: Settings | None = None) -> FastAPI:
    # Imported for the side effect: these modules register the graphs and handlers, and
    # the API now reports what each entrypoint dispatches on (`observe._modes`) and
    # validates document sets against the registries (`/v1/control/specs/validate`).
    # An API process that skipped them would answer "this graph has no modes" and
    # "no graph registered as marketing_head@1" — both wrong, and both wrong quietly.
    # The same list `runtime.worker.main` imports, for the same reason.
    import runtime.graphs.bot_agent
    import runtime.graphs.delegator
    import runtime.graphs.department
    import runtime.graphs.echo_agent
    import runtime.handlers  # noqa: F401  registers analytics@1, hasher@1

    app = FastAPI(title="agent-org-runtime", version="0.1.0", lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings

    # Read-only. Mounted here rather than kept in a second process so that the
    # viewer reads through the same session factory and the same connection pool
    # the control surface does — a second process would be a second answer to
    # "what is the state right now".
    app.include_router(observe_router)

    # Write. Mounted beside the reader rather than folded into it, because the whole
    # value of `observe.py`'s first rule — *every statement here is a SELECT* — is that
    # it is checkable by opening the file. A control endpoint added to that module
    # would end that, and `test_api_observe.py`'s "the surface is read-only" test would
    # start being about a convention instead of about a fact.
    app.include_router(control_router)

    # Bots: the chat surface and its computer proxy. Writes go through `BotManager`, and
    # every run it creates goes through `RunService.start_run()` like the rest.
    app.include_router(bots_router)
    app.include_router(bots_computer_router)
    app.include_router(bots_vault_router)
    # Bytes into a team's drive (attachments, uploads) and back out (`/raw`).
    app.include_router(attachments_router)
    # The computer's shared /workspace and the person's sandboxed shell.
    app.include_router(workspace_router)
    # Group chats between the person and their bots, and reactions on any message.
    app.include_router(groups_router)
    app.include_router(reactions_router)
    # Connectors: MCP servers the organization connected, and the marketplace's.
    app.include_router(connectors_router)
    app.include_router(connectors_market_router)
    # Routines: a bot's own (`/v1/bots/{id}/routines`, behind the UI's proxy), and the
    # webhook that starts an event routine (`/v1/hooks`, which is not — it is called
    # from outside, and authenticated by its token and the sender's signature).
    app.include_router(routines_router)
    app.include_router(hooks_router)
    # Skills: the organization's library, the packaged ones, and teaching by
    # demonstration (`/v1/bots/{id}/teach`), which drives the computer's recorder.
    app.include_router(skills_router)
    app.include_router(marketplace_router)
    app.include_router(teach_router)

    def uow_factory(request: Request) -> UnitOfWorkFactory:
        factory: UnitOfWorkFactory = request.app.state.uow
        return factory

    def run_service(request: Request) -> RunService:
        service: RunService = request.app.state.service
        return service

    def organization_id(request: Request) -> OrganizationId:
        raw = request.headers.get(ORG_HEADER)
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{ORG_HEADER} header is required",
            )
        try:
            return OrganizationId(UUID(raw))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{ORG_HEADER} must be a UUID",
            ) from exc

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        """Reports what is actually reachable, not merely that the process is up.

        A health check that returns 200 while the database is unreachable is worse
        than none: it tells the orchestrator to keep sending traffic.
        """
        db_ok = redis_ok = False
        try:
            async with request.app.state.uow() as uow:
                await uow.session.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.warning("health.database_unreachable")
        try:
            await request.app.state.streams.client.ping()
            redis_ok = True
        except Exception:
            log.warning("health.redis_unreachable")
        return HealthResponse(
            status="ok" if db_ok and redis_ok else "degraded",
            database=db_ok,
            redis=redis_ok,
            version=app.version,
        )

    @app.post(
        "/v1/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(
        body: StartRunBody,
        org: OrganizationId = Depends(organization_id),
        service: RunService = Depends(run_service),
    ) -> StartRunResponse:
        """202, not 201. The run is accepted, not finished — and a caller that gets
        a 201 tends to assume it can read the result on the next line."""
        if body.parent_run_id is not None:
            # M5. Until M5 this field created a child run with none of the checks a
            # child needs — no cycle check, no privilege subset, no subtree ceiling —
            # because those checks did not exist. They do now, they live behind
            # `delegate()`, and an HTTP caller that could reach around them would be
            # the one hole in §4's list. So it is refused rather than quietly ignored:
            # a parameter that stops meaning what it said should say so.
            raise HTTPException(
                status_code=400,
                detail=(
                    "parent_run_id is not accepted here. A child run is created by "
                    "delegation, from inside its parent, so that the cycle, privilege, "
                    "scope and subtree-budget checks apply to it."
                ),
            )
        # The mapping lives in `errors.py` because `/v1/control` needs the same one,
        # and a second hand-written chain would eventually disagree with this one about
        # a status. It is a superset of what this endpoint used to have: the added
        # branches are config-plane errors `start_run` cannot raise.
        with http_errors():
            result = await service.start_run(
                StartRunRequest(
                    organization_id=org,
                    actor_name=body.actor,
                    input=body.input,
                    idempotency_key=body.idempotency_key,
                    session_id=SessionId(body.session_id) if body.session_id else None,
                    priority=body.priority,
                    deadline_s=body.deadline_s,
                    durability=body.durability,
                )
            )

        return StartRunResponse(
            run_id=result.run_id,
            status=result.status.value,
            spec_hash=result.spec_hash,
            created=result.created,
        )

    @app.get("/v1/runs/{run_id}", response_model=RunView)
    async def get_run(
        run_id: UUID,
        org: OrganizationId = Depends(organization_id),
        factory: UnitOfWorkFactory = Depends(uow_factory),
    ) -> RunView:
        async with factory() as uow:
            run = await uow.runs.get(RunId(run_id))
            if run is None or run.organization_id != org:
                raise HTTPException(status_code=404, detail=f"no run {run_id}")
            spec_row = await uow.runs.get_spec(RunId(run_id))
            effects = await uow.effects.for_run(run_id)
            artifacts = await uow.artifacts.for_run(run_id)
            cost = await uow.budget.spend_for_run(run_id)
            events = await uow.outbox.events_for_run(run_id)
            version = (
                await uow.session.execute(
                    text("SELECT version FROM actor_versions WHERE id = :id"),
                    {"id": run.actor_version_id},
                )
            ).scalar_one()

        output: dict[str, Any] | None = None
        for event in reversed(events):
            if event["topic"] in ("run.succeeded", "run.failed"):
                output = event["payload"].get("output")
                break

        return RunView(
            run_id=run.id,
            organization_id=run.organization_id,
            root_run_id=run.root_run_id,
            parent_run_id=run.parent_run_id,
            actor_id=run.actor_id,
            actor_version=int(version),
            status=run.status,
            status_reason=run.status_reason,
            spec_hash=spec_row.spec_hash if spec_row else "",
            fence=run.fence,
            lease_expiries=run.lease_expiries,
            priority=run.priority,
            created_at=run.created_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            output=output,
            effects=[
                EffectView(
                    logical_call_id=e.logical_call_id,
                    tool=f"{e.tool_name}@{e.tool_version}",
                    status=e.status.value,
                    attempts=e.attempts,
                    recovery_policy=e.recovery_policy,
                )
                for e in effects
            ],
            artifacts=[
                ArtifactView(
                    artifact_id=a.artifact_id,
                    version=a.version,
                    uri=a.uri,
                    size_bytes=a.size_bytes,
                    sha256=a.sha256,
                    content_type=a.content_type,
                )
                for a in artifacts
            ],
            cost_cents=cost,
        )

    @app.get("/v1/runs/{run_id}/stream")
    async def stream_run(
        run_id: UUID,
        request: Request,
        org: OrganizationId = Depends(organization_id),
        factory: UnitOfWorkFactory = Depends(uow_factory),
        after: int = Query(default=0, ge=0, description="Last event id already seen"),
    ) -> StreamingResponse:
        """SSE tail, backed by Postgres.

        Polling `events` rather than subscribing to Redis is a deliberate trade: a
        little latency for the property that a client can reconnect with
        `?after=<id>` and get every event it missed, including across a Redis wipe.
        """

        async def _events() -> AsyncIterator[str]:
            cursor = after
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                async with factory() as uow:
                    run = await uow.runs.get(RunId(run_id))
                    if run is None or run.organization_id != org:
                        yield _sse("error", {"detail": f"no run {run_id}"})
                        return
                    batch = await uow.outbox.events_for_run(run_id, after_id=cursor)

                for event in batch:
                    cursor = int(event["id"])
                    yield _sse(event["topic"], event["payload"], event_id=cursor)

                if run.status in ("SUCCESS", "FAILED", "CANCELLED", "ABANDONED") and not batch:
                    yield _sse("done", {"status": run.status}, event_id=cursor)
                    return

                idle = 0.0 if batch else min(idle + 0.1, 1.0)
                await asyncio.sleep(0.1 if batch else max(idle, 0.1))

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


app = create_app()
