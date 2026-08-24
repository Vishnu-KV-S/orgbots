import type { ComponentType } from "react";
import type { NodeKind } from "@/lib/types";
import type { PanelProps } from "../types";
import { ActorPanel } from "./ActorPanel";
import { DepartmentPanel } from "./DepartmentPanel";
import { OrganizationPanel } from "./OrganizationPanel";
import { GoalPanel, ProjectPanel } from "./PlanPanels";

/**
 * Node kind → the panel that explains it.
 *
 * The inspector looks a panel up here instead of running through a chain of
 * `node.type === …` checks, so a new kind is a new file and a line, and a kind
 * with no panel yet renders as nothing rather than as a broken screen.
 */
export const PANELS: Partial<Record<NodeKind, ComponentType<PanelProps>>> = {
  organization: OrganizationPanel,
  department: DepartmentPanel,
  actor: ActorPanel,
  goal: GoalPanel,
  project: ProjectPanel,
};

export function panelFor(kind: NodeKind): ComponentType<PanelProps> | null {
  return PANELS[kind] ?? null;
}
