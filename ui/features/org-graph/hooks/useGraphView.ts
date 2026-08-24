"use client";

import type { Edge, Node } from "@xyflow/react";
import { useMemo } from "react";
import type { GraphEdge, GraphNode, OrgGraph } from "@/lib/types";
import { toFlowEdge } from "../lib/edges";
import { layout } from "../lib/layout";
import { NODE_SIZE, PLAN_KINDS } from "../nodes/registry";
import type { FlowNodeData } from "../nodes/types";

/**
 * Wire graph → what React Flow draws: filter, lay out, wrap.
 *
 * Kept out of the canvas component because it is pure — same graph and same
 * filters give the same nodes and edges — and because that is what makes the
 * filters cheap: toggling one re-runs this and nothing else.
 */

export interface GraphFilters {
  /** Goals and projects: the plan, as opposed to the org chart. */
  showPlan: boolean;
  /** Observed delegation traffic. */
  showDelegation: boolean;
}

export interface GraphView {
  nodes: Node<FlowNodeData>[];
  edges: Edge[];
}

export function useGraphView(
  graph: OrgGraph | null,
  filters: GraphFilters,
  selectedId: string | null,
): GraphView {
  const visible = useMemo(() => {
    if (!graph) return { nodes: [] as GraphNode[], edges: [] as GraphEdge[] };
    const kept = filters.showPlan
      ? graph.nodes
      : graph.nodes.filter((node) => !PLAN_KINDS.includes(node.type));
    const ids = new Set(kept.map((node) => node.id));
    return {
      nodes: kept,
      // An edge to a hidden node would be drawn to nowhere.
      edges: graph.edges.filter(
        (edge) =>
          ids.has(edge.source) &&
          ids.has(edge.target) &&
          (filters.showDelegation || edge.kind !== "delegates"),
      ),
    };
  }, [graph, filters.showPlan, filters.showDelegation]);

  const positions = useMemo(
    () => layout(visible.nodes, visible.edges, NODE_SIZE),
    [visible.nodes, visible.edges],
  );

  const nodes = useMemo(
    () =>
      visible.nodes.map<Node<FlowNodeData>>((node) => {
        const at = positions.get(node.id);
        return {
          id: node.id,
          type: node.type,
          position: { x: at?.x ?? 0, y: at?.y ?? 0 },
          data: { node },
          selected: node.id === selectedId,
          // Width only: the node sizes itself vertically, so nothing is clipped.
          style: { width: NODE_SIZE[node.type].width },
        };
      }),
    [visible.nodes, positions, selectedId],
  );

  const edges = useMemo(() => visible.edges.map(toFlowEdge), [visible.edges]);

  return { nodes, edges };
}
