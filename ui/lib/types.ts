/**
 * Wire types for `/v1/observe` and `/v1/control`. Hand-written to match
 * `runtime/api/observe.py` and `runtime/api/control.py` — if you change a payload
 * there, change it here. There is no generator, and a mismatch shows up as
 * `undefined` on a screen rather than as a build failure.
 */

export type OrgStatus = "running" | "idle" | "halted";

export interface OrganizationSummary {
  id: string;
  name: string;
  created_at: string | null;
  status: OrgStatus;
  departments: number;
  actors: number;
  runs_active: number;
  runs_total: number;
  runs_failed: number;
  tasks_open: number;
  goals: number;
  kill_switches: number;
  last_activity_at: string | null;
}

export type NodeKind = "organization" | "department" | "actor" | "goal" | "project";

export type EdgeKind =
  | "contains"
  | "heads"
  | "member"
  | "reports_to"
  | "pursues"
  | "project"
  | "owns"
  | "delegates";

export interface Ceilings {
  max_depth?: number;
  max_llm_calls?: number;
  max_tool_calls?: number;
  max_cost_cents?: number;
  max_wall_clock_s?: number;
}

export interface TriggerView {
  key: string;
  /** The actor whose schedule this is. Present because a department node pools
   * its members' triggers, and a list of crons with no owner is unusable. */
  actor: string;
  cron: string;
  timezone: string;
  active: boolean;
  last_evaluated_at: string | null;
}

export interface ActorNodeData {
  id: string;
  kind: string;
  role: string | null;
  rank: number | null;
  department: string | null;
  reports_to: string | null;
  active: boolean;
  version: number | null;
  spec_hash: string | null;
  graph_ref: string | null;
  handler_ref: string | null;
  /** What `input.mode` may be, read off the entrypoint's own branch. Empty means
   * the graph or handler declared none — offer free text, not an empty select. */
  modes: string[];
  tools: string[];
  model_profiles: Record<string, string>;
  ceilings: Ceilings;
  triggers: TriggerView[];
  spend_cents: number;
  runs: { active: number; running: number; success: number; failed: number; total: number };
  last_run_at: string | null;
  tasks: { open: number; accepted: number; rejected: number; total: number };
  status: "running" | "queued" | "idle";
}

export type DepartmentState = "running" | "paused" | "stopped";

export interface KillSwitchView {
  scope_type: string;
  scope_id: string | null;
  mode: string;
  reason: string | null;
  engaged_by: string | null;
  engaged_at: string | null;
}

export interface DepartmentNodeData {
  id: string;
  description: string;
  head: string | null;
  members: number;
  runs_active: number;
  /** Derived, never stored: stopped beats running beats paused. See
   * `observe._department_state`. */
  state: DepartmentState;
  triggers: TriggerView[];
  /** The switch that stops this department, org-scoped or department-scoped. */
  kill_switch: KillSwitchView | null;
}

export interface OrganizationNodeData {
  id: string;
  created_at: string | null;
  departments: number;
  actors: number;
  runs_active: number;
  spend_cents: number;
  kill_switches: KillSwitchView[];
}

export interface GoalNodeData {
  id: string;
  statement: string;
  horizon: string;
  active: boolean;
}

export interface ProjectNodeData {
  id: string;
  description: string;
  active: boolean;
  owner: string | null;
}

export type GraphNodeData =
  | OrganizationNodeData
  | DepartmentNodeData
  | ActorNodeData
  | GoalNodeData
  | ProjectNodeData;

export interface GraphNode {
  id: string;
  type: NodeKind;
  label: string;
  data: GraphNodeData;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  data?: { calls: number; refused: number };
}

export interface OrgGraph {
  organization: { id: string; name: string; created_at: string | null; status: OrgStatus };
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface RunSummary {
  id: string;
  actor: string;
  department: string | null;
  status: string;
  status_reason: string | null;
  parent_run_id: string | null;
  root_run_id: string;
  task_id: string | null;
  priority: number;
  depth: number;
  cost_cents: number;
  created_at: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface TaskSummary {
  id: string;
  title: string;
  objective: string;
  status: string;
  outcome: string | null;
  outcome_reason: string | null;
  assignee: string | null;
  rework_count: number;
  created_at: string | null;
  submitted_at: string | null;
  closed_at: string | null;
  due_at: string | null;
}

export interface EventRow {
  id: number;
  topic: string;
  payload: Record<string, unknown>;
  run_id: string | null;
  actor: string | null;
  created_at: string | null;
}

export interface Activity {
  runs: RunSummary[];
  tasks: TaskSummary[];
  events: EventRow[];
}

export interface ActorDetail {
  id: string;
  organization_id: string;
  name: string;
  kind: string;
  role: string | null;
  department: string | null;
  reports_to: string | null;
  reports: Array<{ id: string; name: string }>;
  active: boolean;
  created_at: string | null;
  version: number | null;
  spec_hash: string | null;
  spec: Record<string, unknown> | null;
  versions: Array<{ version: number; spec_hash: string; created_at: string | null }>;
  runs: RunSummary[];
}

export interface EffectView {
  logical_call_id: string;
  tool: string;
  status: string;
  attempts: number;
  recovery_policy: string;
}

export interface ArtifactView {
  artifact_id: string;
  version: number;
  uri: string;
  size_bytes: number;
  sha256: string;
  content_type: string;
}

export interface RunDetail extends RunSummary {
  organization_id: string;
  fence: number;
  lease_expiries: number;
  thread_id: string;
  output: Record<string, unknown> | null;
  events: Array<{ id: number; topic: string; payload: Record<string, unknown>; created_at: string }>;
  children: RunSummary[];
  effects: EffectView[];
  artifacts: ArtifactView[];
}

/* --------------------------------------------------------------------------
 * `/v1/control`. Everything below changes something, which is the difference
 * between these types and the ones above.
 * ------------------------------------------------------------------------ */

export interface SpecFileEntry {
  path: string;
  name: string;
  size: number;
}

export interface SpecFolder {
  path: string;
  name: string;
  files: SpecFileEntry[];
  /** Files the loader ignores — a README beside the documents. Listed so nobody
   * spends an afternoon wondering why editing one changed nothing. */
  other: string[];
  kinds: Record<string, number>;
  organization: string | null;
  /** Set when this folder's organization already exists in the database, so the
   * editor can tell "this creates a company" from "this changes one". */
  organization_id: string | null;
  /** A parse failure. The folder still lists — that is exactly when somebody
   * needs to open it. */
  error: string | null;
}

export interface SpecRoot {
  path: string;
  exists: boolean;
  folders: SpecFolder[];
  /** YAML sitting loose at the root of a spec root, like `config/agents.example.yaml`.
   * Not a company; not compiled. */
  loose: string[];
}

export interface SpecListing {
  roots: SpecRoot[];
  /** `RUNTIME_SPEC_EDITABLE`. False means every write verb answers 403. */
  editable: boolean;
}

export interface SpecFile {
  path: string;
  content: string;
  /** Send this back as `base_sha256` to save. It is what makes a concurrent
   * edit a 409 rather than a silent overwrite. */
  sha256: string;
  size: number;
  modified_at: string;
}

export interface SpecValidation {
  ok: boolean;
  organization: string;
  fingerprint: string;
  documents: number;
  actors: Array<{ name: string; kind: string; spec_hash: string }>;
  counts: { roles: number; grants: number; policies: number; triggers: number };
}

export interface PlanChange {
  kind: string;
  name: string;
  action: string;
  detail?: Record<string, unknown>;
}

export interface PlanResult {
  plan_id: string | null;
  plan_hash: string;
  empty: boolean;
  /** The CLI's own rendering. Shown verbatim: two renderings of one diff is two
   * things that can disagree about what an apply will do. */
  render: string;
  changes: PlanChange[];
  renames: string[][];
  warnings: string[];
  created_at: string | null;
  expires_in_seconds: number;
  expires_at: string | null;
}

export interface ApplyResult {
  applied: boolean;
  detail?: string;
  changed?: number;
  created?: number;
  updated?: number;
  deactivated?: number;
  renamed?: number;
  plan_hash: string;
  versions_published?: Record<string, number>;
  budget_changes?: Array<{ scope: string; before: number; after: number }>;
}

export interface DriftReport {
  clean: boolean;
  render: string;
  findings: Array<{ source: string; subject: string; detail: string }>;
}

export interface ApplyHistory {
  events: Array<{
    kind: string;
    name: string;
    action: string;
    detail: Record<string, unknown> | null;
    applied_by: string;
    created_at: string | null;
  }>;
}

export interface DepartmentTrigger {
  key: string;
  actor: string;
  cron: string;
  timezone: string;
  active: boolean;
  last_evaluated_at: string | null;
}

export interface DepartmentDetail {
  name: string;
  description: string;
  head: string | null;
  parent: string | null;
  active: boolean;
  state: DepartmentState;
  members: string[];
  live_runs: number;
  triggers: DepartmentTrigger[];
  kill_switch: KillSwitchView | null;
}

export interface DepartmentList {
  departments: DepartmentDetail[];
  /** How long a worker may keep running on a stale kill-switch cache. The UI says
   * this out loud rather than letting a stop look like it did not take. */
  propagation_seconds: number;
}

export interface StopResult {
  department: string;
  stopped: boolean;
  mode: string;
  triggers_paused: number;
  propagation_seconds: number;
  detail: string;
}

export interface StartResult {
  department: string;
  kill_switch_disengaged: boolean;
  triggers_resumed: number;
  propagation_seconds: number;
}

export interface TickResult {
  fired: Array<{
    trigger: string;
    scheduled_for: string;
    run_id: string | null;
    skipped: boolean;
  }>;
  dispatched: number;
  detail: string;
}

export interface RunStarted {
  run_id: string;
  status: string;
  spec_hash: string;
  /** False means an existing run was returned: the idempotency key had been used.
   * Worth showing — "nothing happened" and "it worked" look identical otherwise. */
  created: boolean;
  /** False when the run was *refused* — a kill switch, an exhausted budget, a shed
   * priority. The request still succeeded and the run id is still real: a refusal is
   * a `LIMIT_REACHED` row, not an exception, so that it can be reviewed later. Show
   * it as a refusal, not as a start. */
  admitted: boolean;
  /** `KILL_SWITCH`, `POOL_EXHAUSTED`, `PRIORITY_SHED`, `ALLOCATION_CEILING`. "No"
   * without a reason sends an operator to the wrong dashboard. */
  refusal_reason: string | null;
}
