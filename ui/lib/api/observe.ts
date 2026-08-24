import type {
  Activity,
  ActorDetail,
  OrgGraph,
  OrganizationSummary,
  RunDetail,
} from "@/lib/types";
import { OBSERVE_BASE, get, query } from "./client";

/**
 * One function per `/v1/observe` endpoint. Components never build a URL: adding
 * an endpoint is a line here, and a change to the observation surface has
 * exactly one place in the UI that has to follow it.
 */

export const listOrganizations = (signal?: AbortSignal) =>
  get<{ organizations: OrganizationSummary[] }>("/organizations", signal).then(
    (body) => body.organizations,
  );

export const fetchGraph = (orgId: string, signal?: AbortSignal) =>
  get<OrgGraph>(`/organizations/${orgId}/graph`, signal);

export const fetchActivity = (orgId: string, limit = 40, signal?: AbortSignal) =>
  get<Activity>(`/organizations/${orgId}/activity${query({ limit })}`, signal);

export const fetchActor = (actorId: string, signal?: AbortSignal) =>
  get<ActorDetail>(`/actors/${actorId}`, signal);

export const fetchRun = (runId: string, signal?: AbortSignal) =>
  get<RunDetail>(`/runs/${runId}`, signal);

/** SSE is opened by `EventSource`, not `fetch`, so this hands back a URL. */
export const streamUrl = (orgId: string, after: number) =>
  `${OBSERVE_BASE}/organizations/${orgId}/stream${query({ after })}`;
