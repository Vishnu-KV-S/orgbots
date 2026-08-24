"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { AppShell, Crumb, Spacer, TopBar } from "@/components/layout";
import { Chip, ErrorNotice, Pill } from "@/components/ui";
import { EventFeed } from "@/features/events";
import { Inspector } from "@/features/inspector";
import type { ActorNodeData } from "@/lib/types";
import { useToggles } from "@/lib/hooks/useToggles";
import { CanvasToolbar } from "./components/CanvasToolbar";
import { OrgCanvas, type OrgCanvasHandle } from "./components/OrgCanvas";
import { useOrgGraph } from "./hooks/useOrgGraph";

/**
 * One organization: the canvas, the inspector beside it, the event tail under
 * it. This is the composition layer — it owns selection and the view flags and
 * hands each piece exactly what that piece needs. No fetching, no drawing.
 */

const TOGGLES = [
  { key: "plan", label: "goals & projects", title: "show goals and projects" },
  { key: "delegation", label: "delegation", title: "show observed delegation traffic" },
  { key: "feed", label: "events", title: "show the event tail" },
] as const;

export function OrgGraphScreen({ orgId }: { orgId: string }) {
  const [flags, toggle] = useToggles({
    plan: true,
    delegation: true,
    feed: true,
    live: true,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const canvas = useRef<OrgCanvasHandle>(null);

  const { graph, activity, error } = useOrgGraph(orgId, { live: flags.live });

  const filters = useMemo(
    () => ({ showPlan: flags.plan, showDelegation: flags.delegation }),
    [flags.plan, flags.delegation],
  );

  const selected = useMemo(
    () => graph?.nodes.find((node) => node.id === selectedId) ?? null,
    [graph, selectedId],
  );

  // The inspector refers to actors by name (`reports_to`, a department head);
  // the canvas works in node ids. Resolving that is this layer's job.
  const focusActor = useCallback(
    (name: string) => {
      const target = graph?.nodes.find((node) => node.type === "actor" && node.label === name);
      if (!target) return;
      setSelectedId(target.id);
      canvas.current?.focusNode(target.id);
    },
    [graph],
  );

  const liveRuns = useMemo(
    () =>
      graph?.nodes
        .filter((node) => node.type === "actor")
        .reduce((sum, node) => sum + (node.data as ActorNodeData).runs.active, 0) ?? 0,
    [graph],
  );

  return (
    <AppShell
      bar={
        <TopBar>
          <Crumb separator>{graph?.organization.name ?? "…"}</Crumb>
          {graph && <Pill status={graph.organization.status} />}
          {liveRuns > 0 && <Chip tone="ok">{liveRuns} live runs</Chip>}
          <Spacer />
          <CanvasToolbar
            toggles={[...TOGGLES, { key: "live", label: flags.live ? "live" : "paused" }]}
            values={flags}
            onToggle={toggle}
            actions={[{ label: "relayout", onClick: () => canvas.current?.relayout() }]}
          />
        </TopBar>
      }
      footer={
        flags.feed && (
          <EventFeed orgId={orgId} seed={activity?.events ?? []} seedReady={activity !== null} />
        )
      }
    >
      {error && <ErrorNotice>{error}</ErrorNotice>}

      <div className="canvas-wrap">
        <OrgCanvas
          ref={canvas}
          graph={graph}
          filters={filters}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
        <Inspector
          node={selected}
          tasks={activity?.tasks ?? []}
          onClose={() => setSelectedId(null)}
          onFocusActor={focusActor}
        />
      </div>
    </AppShell>
  );
}
