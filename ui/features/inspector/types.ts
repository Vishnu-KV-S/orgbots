import type { GraphNode, RunDetail, TaskSummary } from "@/lib/types";

/**
 * What every inspector panel is handed. It is one object rather than a growing
 * list of props so that adding something a single panel needs does not mean
 * editing the signature of all of them.
 */
export interface InspectorContext {
  /** The organization being inspected. Every control-surface call is scoped by it,
   * and until the panels could act, nothing here needed to know which company it
   * was looking at. */
  orgId: string;
  /** The organization's open/recent tasks, already fetched by the screen. */
  tasks: TaskSummary[];
  /** Select the actor with this name and fly the canvas to it. */
  onFocusActor: (name: string) => void;
  /** Drill into a run. The inspector swaps to the run view. */
  onOpenRun: (run: RunDetail) => void;
  /** Re-read the graph now, outside the 3s poll.
   *
   * Called after a successful action instead of patching local state. An optimistic
   * update here would be a second answer to what the organization is, and it would
   * be contradicted by the next poll — which is worse than a beat of latency,
   * because the disagreement is what somebody would remember. */
  onRefresh: () => void;
}

export interface PanelProps {
  node: GraphNode;
  context: InspectorContext;
}
