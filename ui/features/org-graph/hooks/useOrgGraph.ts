"use client";

import { useCallback } from "react";
import { fetchActivity, fetchGraph } from "@/lib/api";
import { useResource } from "@/lib/hooks/useResource";
import type { Activity, OrgGraph } from "@/lib/types";

/**
 * The org screen's data: the shape (graph) and the recent past (activity), on
 * one poll. They are fetched together because they are drawn together — two
 * timers would show a node's run count and the tasks under it a beat apart.
 */

const REFRESH_MS = 3000;
const ACTIVITY_LIMIT = 60;

export interface OrgGraphView {
  graph: OrgGraph | null;
  activity: Activity | null;
  error: string | null;
}

export function useOrgGraph(orgId: string, { live }: { live: boolean }): OrgGraphView {
  const fetcher = useCallback(
    async (signal: AbortSignal) => {
      const [graph, activity] = await Promise.all([
        fetchGraph(orgId, signal),
        fetchActivity(orgId, ACTIVITY_LIMIT, signal),
      ]);
      return { graph, activity };
    },
    [orgId],
  );

  const { data, error } = useResource(fetcher, { intervalMs: REFRESH_MS, enabled: live });

  return { graph: data?.graph ?? null, activity: data?.activity ?? null, error };
}

export { REFRESH_MS as ORG_REFRESH_MS };
