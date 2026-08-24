import type { EdgeKind, GraphEdge, GraphNode, NodeKind } from "@/lib/types";
import type { NodeSize } from "../nodes/types";

/**
 * A tidy top-down tree layout.
 *
 * React Flow places nothing on its own, and the alternative — a force
 * simulation — gives an org chart that is a different shape on every paint. An
 * org chart's whole value is that the same organization looks the same twice,
 * so this is deterministic: leaves take the next free column, parents centre
 * over their children.
 *
 * The graph is not a tree — an actor can be a department member *and* a project
 * owner. So each node keeps exactly one **primary parent**, chosen by the
 * precedence below, and every other edge is drawn without influencing position.
 *
 * Node sizes come in as an argument rather than being imported: this file knows
 * about geometry, not about which components exist.
 */

const PARENT_PRECEDENCE: EdgeKind[] = [
  "reports_to",
  "heads",
  "member",
  "contains",
  "project",
  "pursues",
  "owns",
];

/** Structural edges lay the tree out; `delegates` is observed traffic drawn over it. */
const STRUCTURAL = new Set<EdgeKind>(PARENT_PRECEDENCE);

const COLUMN = 300;
const ROW = 230;

export interface Positioned {
  id: string;
  x: number;
  y: number;
}

export function layout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  sizes: Record<NodeKind, NodeSize>,
): Map<string, Positioned> {
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // Pick each node's primary parent by precedence, ignoring edges to nodes that
  // are not in the payload and any edge that would make a node its own ancestor.
  const primary = new Map<string, string>();
  for (const kind of PARENT_PRECEDENCE) {
    for (const edge of edges) {
      if (edge.kind !== kind) continue;
      if (!byId.has(edge.source) || !byId.has(edge.target)) continue;
      if (primary.has(edge.target) || edge.source === edge.target) continue;
      primary.set(edge.target, edge.source);
    }
  }
  for (const [child, parent] of [...primary]) {
    if (createsCycle(child, parent, primary)) primary.delete(child);
  }

  const children = new Map<string, string[]>();
  for (const node of nodes) children.set(node.id, []);
  for (const [child, parent] of primary) children.get(parent)?.push(child);

  // Stable sibling order: organizations first, then departments, then actors by
  // role rank (already the server's order), then goals and projects.
  const order = new Map(nodes.map((n, index) => [n.id, index]));
  for (const list of children.values()) {
    list.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  }

  const roots = nodes.filter((n) => !primary.has(n.id)).map((n) => n.id);
  const positions = new Map<string, Positioned>();
  let cursor = 0;

  const place = (id: string, depth: number): number => {
    const kids = children.get(id) ?? [];
    let x: number;
    if (kids.length === 0) {
      x = cursor;
      cursor += 1;
    } else {
      const spans = kids.map((kid) => place(kid, depth + 1));
      x = (spans[0] + spans[spans.length - 1]) / 2;
    }
    const size = sizes[byId.get(id)?.type ?? "actor"];
    positions.set(id, {
      id,
      x: x * COLUMN - size.width / 2,
      y: depth * ROW,
    });
    return x;
  };

  for (const root of roots) {
    place(root, 0);
    cursor += 0.6; // a gutter between disconnected roots
  }

  return positions;
}

function createsCycle(child: string, parent: string, primary: Map<string, string>): boolean {
  let cursor: string | undefined = parent;
  const seen = new Set<string>([child]);
  while (cursor) {
    if (seen.has(cursor)) return true;
    seen.add(cursor);
    cursor = primary.get(cursor);
  }
  return false;
}

export function isStructural(kind: EdgeKind): boolean {
  return STRUCTURAL.has(kind);
}
