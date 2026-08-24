"use client";

import type { NodeProps } from "@xyflow/react";
import { BadgeRow, Chip, Pill } from "@/components/ui";
import { cents, relative } from "@/lib/format";
import type { ActorNodeData } from "@/lib/types";
import { NodeFrame, NodeKindLabel, NodeName, NodeSub } from "./NodeFrame";
import { graphNodeOf } from "./types";

/**
 * An actor shows what you would otherwise have to click to find out: whether it
 * is working, what it costs, and what it is allowed to do. The inspector is for
 * the detail; the canvas is for the shape and the pulse.
 */
export function ActorNode({ data, selected }: NodeProps) {
  const node = graphNodeOf(data);
  const d = node.data as ActorNodeData;
  const kind = d.kind === "llm_agent" ? "llm agent" : d.kind.replace(/_/g, " ");

  return (
    <NodeFrame kind="actor" live={d.status !== "idle"} selected={selected}>
      <NodeKindLabel>
        {kind}
        {d.role ? ` · ${d.role}` : ""}
      </NodeKindLabel>
      <NodeName>{node.label}</NodeName>

      <BadgeRow>
        <Pill status={d.status} />
        {d.triggers.length > 0 && (
          <Chip tone="warn" title="cron triggers">
            ⏱ {d.triggers.length}
          </Chip>
        )}
      </BadgeRow>

      <BadgeRow>
        {d.runs.total > 0 ? (
          <>
            <Chip tone="ok">{d.runs.success} ok</Chip>
            {d.runs.failed > 0 && <Chip tone="bad">{d.runs.failed} failed</Chip>}
          </>
        ) : (
          <Chip>no runs</Chip>
        )}
        {d.tasks.open > 0 && <Chip tone="warn">{d.tasks.open} open</Chip>}
        <Chip>{cents(d.spend_cents)}</Chip>
      </BadgeRow>

      <NodeSub>
        {d.tools.length > 0 ? d.tools.join("  ") : "no tools"}
        {d.last_run_at ? ` · ran ${relative(d.last_run_at)}` : ""}
      </NodeSub>
    </NodeFrame>
  );
}
