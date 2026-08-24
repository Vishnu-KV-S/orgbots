import type {
  ApplyResult,
  DepartmentList,
  DriftReport,
  PlanResult,
  RunStarted,
  SpecFile,
  SpecListing,
  SpecValidation,
  StartResult,
  StopResult,
  TickResult,
  ApplyHistory,
} from "@/lib/types";
import { del, getControl, post, put, query } from "./client";

/**
 * One function per `/v1/control` endpoint, exactly as `observe.ts` does for the
 * read surface. Components never build a URL: adding an endpoint is a line here,
 * and a change to the control surface has one place in the UI that follows it.
 *
 * Every function in this file changes something. That is the whole difference
 * between this module and its sibling, and it is why they are two files.
 */

// --- the spec files ------------------------------------------------------------

export const listSpecs = (signal?: AbortSignal) =>
  getControl<SpecListing>("/specs", signal);

export const readSpecFile = (path: string, signal?: AbortSignal) =>
  getControl<SpecFile>(`/specs/file${query({ path })}`, signal);

/** Save an existing file. `baseSha256` is the digest the editor opened; a
 * mismatch is a 409 rather than a silent overwrite of somebody else's work. */
export const writeSpecFile = (path: string, content: string, baseSha256: string) =>
  put<{ path: string; sha256: string; documents: number }>("/specs/file", {
    path,
    content,
    base_sha256: baseSha256,
  });

export const createSpecFile = (path: string, content: string) =>
  post<{ path: string; sha256: string; documents: number }>("/specs/file", {
    path,
    content,
  });

export const deleteSpecFile = (path: string, baseSha256: string) =>
  del<{ path: string; deleted: boolean }>(`/specs/file${query({ path, base_sha256: baseSha256 })}`);

// --- validate, plan, apply -----------------------------------------------------

export const validateSpec = (path: string, organizationName?: string) =>
  post<SpecValidation>("/specs/validate", {
    path,
    organization_name: organizationName ?? null,
  });

export interface PlanOptions {
  renames?: Record<string, string>;
  allowReplace?: boolean;
}

export const planSpec = (orgId: string, path: string, options: PlanOptions = {}) =>
  post<PlanResult>(`/organizations/${orgId}/plan`, {
    path,
    renames: options.renames ?? {},
    allow_replace: options.allowReplace ?? false,
  });

export const applySpec = (
  orgId: string,
  path: string,
  planId: string | null,
  options: PlanOptions = {},
) =>
  post<ApplyResult>(`/organizations/${orgId}/apply`, {
    path,
    plan_id: planId,
    renames: options.renames ?? {},
    allow_replace: options.allowReplace ?? false,
  });

export const specDrift = (orgId: string, path: string) =>
  post<DriftReport>(`/organizations/${orgId}/drift`, { path });

export const applyHistory = (orgId: string, signal?: AbortSignal) =>
  getControl<ApplyHistory>(`/organizations/${orgId}/history`, signal);

// --- operating -----------------------------------------------------------------

export const listDepartments = (orgId: string, signal?: AbortSignal) =>
  getControl<DepartmentList>(`/organizations/${orgId}/departments`, signal);

export const stopDepartment = (
  orgId: string,
  name: string,
  mode: "drain" | "halt",
  reason: string,
) => post<StopResult>(`/organizations/${orgId}/departments/${name}/stop`, { mode, reason });

export const startDepartment = (orgId: string, name: string, reason: string) =>
  post<StartResult>(`/organizations/${orgId}/departments/${name}/start`, { reason });

export const tickOrganization = (orgId: string) =>
  post<TickResult>(`/organizations/${orgId}/tick`);

export const startActorRun = (
  orgId: string,
  actor: string,
  body: { mode?: string; input?: Record<string, unknown>; idempotencyKey?: string },
) =>
  post<RunStarted>(`/organizations/${orgId}/actors/${actor}/run`, {
    mode: body.mode ?? "",
    input: body.input ?? {},
    idempotency_key: body.idempotencyKey ?? null,
  });
