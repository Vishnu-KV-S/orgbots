"use client";

import { ListRow, StatusTag } from "@/components/ui";
import { fetchRun } from "@/lib/api";
import { cents, duration, relative } from "@/lib/format";
import type { RunDetail, RunSummary } from "@/lib/types";

/** One run in a list. Clicking it fetches the detail and hands it up. */
export function RunRow({ run, onOpen }: { run: RunSummary; onOpen: (run: RunDetail) => void }) {
  const open = async () => {
    try {
      onOpen(await fetchRun(run.id));
    } catch {
      /* the row stays put; the run list is refreshed on the next poll anyway */
    }
  };

  return (
    <ListRow
      onClick={open}
      title={run.actor}
      aside={<StatusTag status={run.status} />}
      meta={`${relative(run.created_at)} · ${duration(run.started_at, run.ended_at)} · ${cents(run.cost_cents)}`}
      note={run.status_reason ?? undefined}
    />
  );
}
