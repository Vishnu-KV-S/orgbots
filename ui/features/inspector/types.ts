import type { GraphNode, RunDetail, TaskSummary } from "@/lib/types";

/**
 * What every inspector panel is handed. It is one object rather than a growing
 * list of props so that adding something a single panel needs does not mean
 * editing the signature of all of them.
 */
export interface InspectorContext {
  /** The organization's open/recent tasks, already fetched by the screen. */
  tasks: TaskSummary[];
  /** Select the actor with this name and fly the canvas to it. */
  onFocusActor: (name: string) => void;
  /** Drill into a run. The inspector swaps to the run view. */
  onOpenRun: (run: RunDetail) => void;
}

export interface PanelProps {
  node: GraphNode;
  context: InspectorContext;
}
