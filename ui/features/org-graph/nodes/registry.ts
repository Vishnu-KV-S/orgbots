import type { NodeTypes } from "@xyflow/react";
import type { NodeKind } from "@/lib/types";
import { ActorNode } from "./ActorNode";
import { DepartmentNode } from "./DepartmentNode";
import { OrganizationNode } from "./OrganizationNode";
import { GoalNode, ProjectNode } from "./PlanNodes";
import type { NodeDefinition, NodeSize } from "./types";

/**
 * The node-kind table.
 *
 * Everything the canvas does per kind — which component draws it, how wide it
 * is, what colour its minimap dot is — is decided here and nowhere else. Adding
 * a kind is a new file next to this one, a line in `NODE_DEFINITIONS`, and a
 * panel in `features/inspector/panels/registry.ts`. No `switch` anywhere has to
 * learn about it.
 *
 * Widths are chosen so a column of them lines up; heights are nominal (see
 * `NodeSize`).
 */
export const NODE_DEFINITIONS: Record<NodeKind, NodeDefinition> = {
  organization: {
    kind: "organization",
    component: OrganizationNode,
    size: { width: 260, height: 116 },
    minimapColor: "#5e5e5e",
  },
  department: {
    kind: "department",
    component: DepartmentNode,
    size: { width: 240, height: 130 },
    minimapColor: "#4a4a4a",
  },
  actor: {
    kind: "actor",
    component: ActorNode,
    size: { width: 236, height: 160 },
    minimapColor: "#6e6e6e",
  },
  goal: {
    kind: "goal",
    component: GoalNode,
    size: { width: 248, height: 120 },
    minimapColor: "#2c2c2c",
  },
  project: {
    kind: "project",
    component: ProjectNode,
    size: { width: 236, height: 110 },
    minimapColor: "#2c2c2c",
  },
};

/** What React Flow wants: kind → component. */
export const nodeTypes: NodeTypes = Object.fromEntries(
  Object.entries(NODE_DEFINITIONS).map(([kind, definition]) => [kind, definition.component]),
) as NodeTypes;

export const NODE_SIZE: Record<NodeKind, NodeSize> = Object.fromEntries(
  Object.entries(NODE_DEFINITIONS).map(([kind, definition]) => [kind, definition.size]),
) as Record<NodeKind, NodeSize>;

export function minimapColor(kind: string | undefined): string {
  return NODE_DEFINITIONS[kind as NodeKind]?.minimapColor ?? "#3a3a3a";
}

/** Kinds that are the *plan* rather than the *org chart* — toggled as a group. */
export const PLAN_KINDS: NodeKind[] = ["goal", "project"];
