"use client";

import { ArrowLeftIcon, XMarkIcon } from "@heroicons/react/24/outline";
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
  /** The organization on screen. The panels can act now, and every control-surface
   * call is scoped by it — so it has to be threaded from the screen that knows it. */
  orgId: string;
  tasks: TaskSummary[];
  onClose: () => void;
  onFocusActor: (name: string) => void;
  /** Re-read the graph after an action, rather than patching state locally. */
  onRefresh: () => void;
}

export function Inspector({
  node,
  orgId,
  tasks,
  onClose,
  onFocusActor,
  onRefresh,
}: InspectorProps) {
  const [run, setRun] = useState<RunDetail | null>(null);

  // Selecting a different node drops back out of whatever run was open under
  // the previous one.
  useEffect(() => setRun(null), [node?.id]);

  const openRun = useCallback((next: RunDetail) => setRun(next), []);
  const context = useMemo<InspectorContext>(
    () => ({ orgId, tasks, onFocusActor, onOpenRun: openRun, onRefresh }),
    [orgId, tasks, onFocusActor, openRun, onRefresh],
  );

  if (!node) return null;

  if (run) {
    return (
      <InspectorFrame
        kind={`run · ${run.actor}`}
        title={<span className="mono-title">{run.id}</span>}
        action={
          <IconButton label="Back to node" onClick={() => setRun(null)}>
            <ArrowLeftIcon />
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
          <XMarkIcon />
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
