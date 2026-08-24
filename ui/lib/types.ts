/** Wire types for `/v1/observe`. Hand-written to match `runtime/api/observe.py`. */

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

export interface DepartmentNodeData {
  id: string;
  description: string;
  head: string | null;
  members: number;
  runs_active: number;
}

export interface OrganizationNodeData {
  id: string;
  created_at: string | null;
  departments: number;
  actors: number;
  runs_active: number;
  spend_cents: number;
  kill_switches: Array<{
    scope_type: string;
    scope_id: string | null;
    mode: string;
    reason: string | null;
    engaged_by: string | null;
    engaged_at: string | null;
  }>;
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
