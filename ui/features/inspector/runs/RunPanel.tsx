"use client";

import { Field, Json, KeyValue, List, ListRow, Section, StatusTag } from "@/components/ui";
import { cents, duration, relative } from "@/lib/format";
import type { RunDetail } from "@/lib/types";
import { RunRow } from "./RunRow";

/**
 * A run is the only thing on this canvas with a story rather than a shape, and
 * that story — events, effects, artifacts, output — is what this panel is.
 */
export function RunPanel({
  run,
  onOpenRun,
}: {
  run: RunDetail;
  onOpenRun: (run: RunDetail) => void;
}) {
  return (
    <>
      <Section title="run">
        <KeyValue>
          <Field label="actor">{run.actor}</Field>
          <Field label="status">
            <StatusTag status={run.status} />
          </Field>
          <Field label="took">{duration(run.started_at, run.ended_at)}</Field>
          <Field label="cost">{cents(run.cost_cents)}</Field>
          <Field label="depth">
            {run.depth} · fence {run.fence} · {run.lease_expiries} lease expiries
          </Field>
          {run.task_id && <Field label="task">{run.task_id}</Field>}
        </KeyValue>
        {run.status_reason && <p className="empty failure">{run.status_reason}</p>}
      </Section>

      {run.children.length > 0 && (
        <Section title={`delegated to (${run.children.length})`}>
          <List>
            {run.children.map((child) => (
              <RunRow key={child.id} run={child} onOpen={onOpenRun} />
            ))}
          </List>
        </Section>
      )}

      <Section title={`events (${run.events.length})`}>
        <List>
          {run.events.map((event) => (
            <ListRow
              key={event.id}
              title={event.topic}
              aside={<span className="row-meta">{relative(event.created_at)}</span>}
            />
          ))}
        </List>
      </Section>

      {run.effects.length > 0 && (
        <Section title={`effects (${run.effects.length})`}>
          <List>
            {run.effects.map((effect) => (
              <ListRow
                key={effect.logical_call_id}
                title={effect.tool}
                aside={<StatusTag status={effect.status} />}
                meta={`${effect.attempts} attempt(s) · ${effect.recovery_policy}`}
              />
            ))}
          </List>
        </Section>
      )}

      {run.artifacts.length > 0 && (
        <Section title={`artifacts (${run.artifacts.length})`}>
          <List>
            {run.artifacts.map((artifact) => (
              <ListRow
                key={`${artifact.artifact_id}:${artifact.version}`}
                title={`${artifact.content_type} v${artifact.version}`}
                aside={<span className="row-meta">{artifact.size_bytes} B</span>}
                meta={artifact.uri}
              />
            ))}
          </List>
        </Section>
      )}

      {run.output && (
        <Section title="output">
          <Json value={run.output} />
        </Section>
      )}
    </>
  );
}
