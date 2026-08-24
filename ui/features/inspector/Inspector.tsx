"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Empty, IconButton } from "@/components/ui";
import type { GraphNode, RunDetail, TaskSummary } from "@/lib/types";
import { panelFor } from "./panels/registry";
import { RunPanel } from "./runs/RunPanel";
import type { InspectorContext } from "./types";

/**
 * The detail panel. It drills one level at a time — node, then run — because a
 * run is the only thing on this canvas with a story rather than a shape.
 *
 * The panel body is looked up in the registry, so this component only owns the
 * frame, the back/close affordance and the node→run transition. It never grows
 * a branch per node kind.
 */

export interface InspectorProps {
  node: GraphNode | null;
  tasks: TaskSummary[];
  onClose: () => void;
  onFocusActor: (name: string) => void;
}

export function Inspector({ node, tasks, onClose, onFocusActor }: InspectorProps) {
  const [run, setRun] = useState<RunDetail | null>(null);

  // Selecting a different node drops back out of whatever run was open under
  // the previous one.
  useEffect(() => setRun(null), [node?.id]);

  const openRun = useCallback((next: RunDetail) => setRun(next), []);
  const context = useMemo<InspectorContext>(
    () => ({ tasks, onFocusActor, onOpenRun: openRun }),
    [tasks, onFocusActor, openRun],
  );

  if (!node) return null;

  if (run) {
    return (
      <InspectorFrame
        kind={`run · ${run.actor}`}
        title={<span className="mono-title">{run.id}</span>}
        action={
          <IconButton label="Back to node" onClick={() => setRun(null)}>
            ←
          </IconButton>
        }
      >
        <RunPanel run={run} onOpenRun={openRun} />
      </InspectorFrame>
    );
  }

  const Panel = panelFor(node.type);

  return (
    <InspectorFrame
      kind={node.type}
      title={node.label}
      action={
        <IconButton label="Close" onClick={onClose}>
          ×
        </IconButton>
      }
    >
      {Panel ? <Panel node={node} context={context} /> : <Empty>Nothing to show for this node.</Empty>}
    </InspectorFrame>
  );
}

function InspectorFrame({
  kind,
  title,
  action,
  children,
}: {
  kind: React.ReactNode;
  title: React.ReactNode;
  action: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <aside className="inspector">
      <div className="inspector-head">
        <div className="inspector-title">
          <div className="node-kind">{kind}</div>
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      <div className="inspector-body">{children}</div>
    </aside>
  );
}
