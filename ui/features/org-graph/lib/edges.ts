import { MarkerType, type BuiltInEdge } from "@xyflow/react";
import { plural } from "@/lib/format";
import type { EdgeKind, GraphEdge } from "@/lib/types";

/**
 * The edge-kind table, and the one function that turns a wire edge into a React
 * Flow edge. Same idea as the node registry: a new edge kind is a line here.
 *
 * `emphasis` is what the org chart proper is drawn in — reporting lines are the
 * spine of the picture and everything else is context around them.
 */

export interface EdgeStyle {
  stroke: string;
  dash?: string;
  emphasis?: boolean;
  animated?: boolean;
  /** Shown in the legend under this label; omitted kinds stay out of it. */
  legend?: string;
}

/**
 * SVG strokes cannot read a CSS custom property through React Flow's inline
 * style, so these are literals — the neutral ramp from `tokens.css`, written
 * out. Nothing here has a hue: an edge's rank is its lightness, and its
 * character is solid vs dashed.
 */
export const EDGE_STYLES: Record<EdgeKind, EdgeStyle> = {
  reports_to: { stroke: "#5c5c5c", emphasis: true, legend: "reports to" },
  heads: { stroke: "#5c5c5c", emphasis: true },
  contains: { stroke: "#343434", legend: "contains" },
  member: { stroke: "#343434" },
  pursues: { stroke: "#2d2d2d", dash: "5 5" },
  project: { stroke: "#2d2d2d", dash: "5 5", legend: "goal → project" },
  owns: { stroke: "#404040", dash: "3 4" },
  // Traffic that actually ran, rather than structure someone configured. It is
  // the top of the ramp, but it is dashed and it moves — the animation does the
  // work, so the stroke does not have to be bright to be found.
  delegates: {
    stroke: "#6e6e6e",
    dash: "2 4",
    animated: true,
    legend: "observed delegation",
  },
};

const FALLBACK: EdgeStyle = EDGE_STYLES.contains;

// `BuiltInEdge` rather than `Edge`: `pathOptions` only exists on the union
// member for the edge type that reads it, and bezier is the one we ask for.
export function toFlowEdge(edge: GraphEdge): BuiltInEdge {
  const style = EDGE_STYLES[edge.kind] ?? FALLBACK;
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    // Bezier rather than stepped elbows: the org chart reads as a flow instead
    // of a circuit diagram. The curvature is pushed above the 0.25 default so
    // the fan-out from a head to its reports is unmistakably a curve, and so
    // that two edges leaving the same handle separate early enough to follow.
    type: "default",
    pathOptions: { curvature: 0.4 },
    animated: style.animated ?? false,
    // Only delegation carries a count, and only because it is traffic that ran
    // rather than structure that was configured.
    label: edge.kind === "delegates" && edge.data ? plural(edge.data.calls, "call") : undefined,
    labelStyle: { fill: "#8a8a8a", fontSize: 10 },
    labelBgStyle: { fill: "#0f0f0f" },
    selectable: false,
    style: {
      stroke: style.stroke,
      strokeWidth: style.emphasis ? 1.6 : 1.2,
      strokeDasharray: style.dash,
    },
    markerEnd: { type: MarkerType.ArrowClosed, color: style.stroke, width: 14, height: 14 },
  };
}
