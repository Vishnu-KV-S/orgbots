"use client";

import type { NodeProps } from "@xyflow/react";
import { BadgeRow, Chip, Pill } from "@/components/ui";
import { cents } from "@/lib/format";
import type { OrganizationNodeData } from "@/lib/types";
import { NodeFrame, NodeKindLabel, NodeName } from "./NodeFrame";
import { graphNodeOf } from "./types";

export function OrganizationNode({ data, selected }: NodeProps) {
  const node = graphNodeOf(data);
  const d = node.data as OrganizationNodeData;
  // A kill switch outranks a live run: the runs still counted here are draining.
  const status = d.kill_switches.length > 0 ? "halted" : d.runs_active ? "running" : "idle";
  return (
    <NodeFrame kind="organization" live={d.runs_active > 0} selected={selected}>
      <NodeKindLabel>organization</NodeKindLabel>
      <NodeName>{node.label}</NodeName>
      <BadgeRow>
        <Pill status={status} />
        <Chip>{d.actors} actors</Chip>
        <Chip>{cents(d.spend_cents)}</Chip>
      </BadgeRow>
    </NodeFrame>
  );
}
