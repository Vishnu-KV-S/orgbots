import type { NodeProps } from "@xyflow/react";
import type { ComponentType } from "react";
import type { GraphNode, NodeKind } from "@/lib/types";

/** What React Flow carries for us in `node.data`: the wire node, untouched. */
export interface FlowNodeData extends Record<string, unknown> {
  node: GraphNode;
}

export interface NodeSize {
  width: number;
  /**
   * Nominal only. Width is applied to the node; nodes size themselves
   * vertically to their content, so this is the figure used to centre the
   * viewport on one.
   */
  height: number;
}

/** Everything the canvas needs to know about one kind of node. */
export interface NodeDefinition {
  kind: NodeKind;
  component: ComponentType<NodeProps>;
  size: NodeSize;
  /** Its dot in the minimap. */
  minimapColor: string;
}

/** Narrowing helper: pull the typed payload out of a React Flow node. */
export function graphNodeOf(data: unknown): GraphNode {
  return (data as FlowNodeData).node;
}
