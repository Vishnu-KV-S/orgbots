"use client";

import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, type Ref } from "react";
import type { OrgGraph } from "@/lib/types";
import { useGraphView, type GraphFilters } from "../hooks/useGraphView";
import { NODE_SIZE, minimapColor, nodeTypes } from "../nodes/registry";
import type { FlowNodeData } from "../nodes/types";
import { CanvasLegend } from "./CanvasLegend";

/**
 * The canvas itself: React Flow, the viewport rules, and nothing else. It takes
 * a graph and reports clicks; it does not fetch, and it does not know what a
 * side panel is. The two things a parent cannot do from props — re-run the
 * layout, and fly to a node — are exposed on a handle.
 */

export interface OrgCanvasHandle {
  /** Throw away dragged positions and re-run the layout. */
  relayout: () => void;
  /** Select nothing, but centre the viewport on this node. */
  focusNode: (nodeId: string) => void;
}

export interface OrgCanvasProps {
  graph: OrgGraph | null;
  filters: GraphFilters;
  selectedId: string | null;
  onSelect: (nodeId: string | null) => void;
  ref?: Ref<OrgCanvasHandle>;
}

export function OrgCanvas(props: OrgCanvasProps) {
  // React Flow's hooks need a provider above them, and this is the boundary
  // that owns the flow, so the provider belongs here rather than in a page.
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

function Canvas({ graph, filters, selectedId, onSelect, ref }: OrgCanvasProps) {
  const view = useGraphView(graph, filters, selectedId);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { fitView, setCenter } = useReactFlow();
  const fitted = useRef(false);
  const keepPositions = useRef(true);

  // A poll refreshes what a node *says*, never where the viewer put it. Anyone
  // who has dragged a node into place has told us the layout is wrong there, and
  // snapping it back every three seconds would make the canvas unusable while
  // anything is running. "relayout" is the way back.
  useEffect(() => {
    setNodes((current) => {
      const placed = new Map(current.map((node) => [node.id, node.position]));
      const keep = keepPositions.current;
      keepPositions.current = true;
      return view.nodes.map((node) => ({
        ...node,
        position: (keep && placed.get(node.id)) || node.position,
      }));
    });
  }, [view.nodes, setNodes]);

  useEffect(() => setEdges(view.edges), [view.edges, setEdges]);

  // Fit once, on the first paint that has nodes. Refitting on every poll would
  // yank the viewport away from wherever the viewer had panned to.
  useEffect(() => {
    if (fitted.current || nodes.length === 0) return;
    fitted.current = true;
    const timer = setTimeout(() => void fitView({ padding: 0.18, duration: 300 }), 60);
    return () => clearTimeout(timer);
  }, [nodes.length, fitView]);

  const relayout = useCallback(() => {
    keepPositions.current = false;
    setNodes(view.nodes);
    setTimeout(() => void fitView({ padding: 0.18, duration: 300 }), 30);
  }, [view.nodes, setNodes, fitView]);

  const focusNode = useCallback(
    (nodeId: string) => {
      const node = nodes.find((candidate) => candidate.id === nodeId);
      if (!node) return;
      const size = NODE_SIZE[(node.type ?? "actor") as keyof typeof NODE_SIZE] ?? NODE_SIZE.actor;
      setCenter(node.position.x + size.width / 2, node.position.y + size.height / 2, {
        zoom: 1,
        duration: 350,
      });
    },
    [nodes, setCenter],
  );

  useImperativeHandle(ref, () => ({ relayout, focusNode }), [relayout, focusNode]);

  const legendKinds = useMemo(
    () => (filters.showDelegation ? undefined : (["delegates"] as const)),
    [filters.showDelegation],
  );

  return (
    <div className="canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelect(node.id)}
        onPaneClick={() => onSelect(null)}
        nodesConnectable={false}
        edgesFocusable={false}
        proOptions={{ hideAttribution: true }}
        minZoom={0.15}
        maxZoom={2}
      >
        {/* No colours here: the dot grid, the controls and the minimap mask all
            come from the `--xy-*` theme block in canvas.css, which resolves to
            the same tokens as everything else. The per-node minimap dot is the
            exception — it is a callback, not a stylesheet property. */}
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={(node) => minimapColor(node.type)} />
      </ReactFlow>
      <CanvasLegend hide={legendKinds} />
    </div>
  );
}
