"""Wire types.

Separate from the domain specs on purpose. The API surface is a compatibility
promise to callers; `RunSpec` is an internal contract that changes whenever the
runtime needs it to. Collapsing the two makes every internal refactor a breaking
API change.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StartRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=50, ge=0, le=100)
    deadline_s: float | None = Field(default=None, gt=0)
    durability: Literal["sync", "async", "exit"] = "sync"
    parent_run_id: UUID | None = None
    session_id: UUID | None = None


class StartRunResponse(BaseModel):
    run_id: UUID
    status: str
    spec_hash: str
    created: bool
    """False when a repeated idempotency key returned an existing run. Callers that
    retry need to be able to tell — a retry that silently looks like a fresh start
    hides double-submission bugs on their side."""


class EffectView(BaseModel):
    logical_call_id: str
    tool: str
    status: str
    attempts: int
    recovery_policy: str


class ArtifactView(BaseModel):
    artifact_id: UUID
    version: int
    uri: str
    size_bytes: int
    sha256: str
    content_type: str


class RunView(BaseModel):
    run_id: UUID
    organization_id: UUID
    root_run_id: UUID
    parent_run_id: UUID | None
    actor_id: UUID
    actor_version: int
    status: str
    status_reason: str | None
    spec_hash: str
    fence: int
    lease_expiries: int
    priority: int
    created_at: dt.datetime
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    output: dict[str, Any] | None
    effects: list[EffectView]
    artifacts: list[ArtifactView]
    cost_cents: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    redis: bool
    version: str
