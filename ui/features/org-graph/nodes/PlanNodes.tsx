"use client";

import type { NodeProps } from "@xyflow/react";
import type { GoalNodeData, ProjectNodeData } from "@/lib/types";
import { NodeFrame, NodeKindLabel, NodeName, NodeSub } from "./NodeFrame";
import { graphNodeOf } from "./types";

/** Plan nodes: what the org intends, as opposed to what it is made of. */

export function GoalNode({ data, selected }: NodeProps) {
  const node = graphNodeOf(data);
  const d = node.data as GoalNodeData;
  return (
    <NodeFrame kind="goal" selected={selected}>
      <NodeKindLabel>goal · {d.horizon}</NodeKindLabel>
      <NodeName>{node.label}</NodeName>
      <NodeSub>{d.statement}</NodeSub>
    </NodeFrame>
  );
}

export function ProjectNode({ data, selected }: NodeProps) {
  const node = graphNodeOf(data);
  const d = node.data as ProjectNodeData;
  return (
    <NodeFrame kind="project" selected={selected}>
      <NodeKindLabel>project</NodeKindLabel>
      <NodeName>{node.label}</NodeName>
      <NodeSub>{d.description || (d.owner ? `owner: ${d.owner}` : "—")}</NodeSub>
    </NodeFrame>
  );
}
