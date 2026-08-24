"use client";

import { Handle, Position } from "@xyflow/react";
import { cx } from "@/lib/cx";

/**
 * The shell every canvas node shares: the two connection handles React Flow
 * needs, plus the border/glow states. A node component supplies content only,
 * so "what a selected node looks like" is decided once.
 */
export function NodeFrame({
  kind,
  live,
  selected,
  children,
}: {
  kind: string;
  live?: boolean;
  selected?: boolean;
  children: React.ReactNode;
}) {
  return (
    <>
      <Handle type="target" position={Position.Top} />
      <div className={cx("node", kind, live && "live", selected && "selected")}>{children}</div>
      <Handle type="source" position={Position.Bottom} />
    </>
  );
}

/** The small uppercase line above a node's name: what kind of thing this is. */
export function NodeKindLabel({ children }: { children: React.ReactNode }) {
  return <div className="node-kind">{children}</div>;
}

export function NodeName({ children }: { children: React.ReactNode }) {
  return <div className="node-name">{children}</div>;
}

/** The dim line at the bottom: tools, descriptions, last-run time. */
export function NodeSub({ children }: { children: React.ReactNode }) {
  return <div className="node-sub">{children}</div>;
}
