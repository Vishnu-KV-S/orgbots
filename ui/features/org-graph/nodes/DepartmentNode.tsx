"use client";

import type { NodeProps } from "@xyflow/react";
import { BadgeRow, Chip } from "@/components/ui";
import type { DepartmentNodeData } from "@/lib/types";
import { NodeFrame, NodeKindLabel, NodeName, NodeSub } from "./NodeFrame";
import { graphNodeOf } from "./types";

export function DepartmentNode({ data, selected }: NodeProps) {
  const node = graphNodeOf(data);
  const d = node.data as DepartmentNodeData;
  return (
    <NodeFrame kind="department" live={d.runs_active > 0} selected={selected}>
      <NodeKindLabel>department</NodeKindLabel>
      <NodeName>{node.label}</NodeName>
      <NodeSub>{d.description || "no description"}</NodeSub>
      <BadgeRow>
        <Chip>{d.members} members</Chip>
        {d.head && <Chip>head: {d.head}</Chip>}
      </BadgeRow>
    </NodeFrame>
  );
}
