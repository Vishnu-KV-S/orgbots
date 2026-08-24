"use client";

import { useCallback } from "react";
import {
  BadgeRow,
  Chip,
  Empty,
  Field,
  Fields,
  InlineButton,
  KeyValue,
  List,
  ListRow,
  Loading,
  Section,
  StatusTag,
} from "@/components/ui";
import { fetchActor } from "@/lib/api";
import { cents, relative } from "@/lib/format";
import { useResource } from "@/lib/hooks/useResource";
import type { ActorNodeData } from "@/lib/types";
import { RunControl } from "../components/RunControl";
import { RunRow } from "../runs/RunRow";
import type { PanelProps } from "../types";

export function ActorPanel({ node, context }: PanelProps) {
  const d = node.data as ActorNodeData;

  // The graph payload carries the actor's shape; its run history is a second
  // request, made only when someone actually opens the panel.
  const fetcher = useCallback((signal: AbortSignal) => fetchActor(d.id, signal), [d.id]);
  const { data: detail, error, loading } = useResource(fetcher);

  const mine = context.tasks.filter((task) => task.assignee === node.label);

  return (
    <>
      <Section title="identity">
        <KeyValue>
          <Field label="kind">{d.kind}</Field>
          <Field label="role">
            {d.role ?? "unplaced"}
            {d.rank !== null ? ` (rank ${d.rank})` : ""}
          </Field>
          <Field label="department">{d.department ?? "—"}</Field>
          <Field label="reports to">
            {d.reports_to ? (
              <InlineButton onClick={() => context.onFocusActor(d.reports_to!)}>
                {d.reports_to}
              </InlineButton>
            ) : (
              "nobody"
            )}
          </Field>
          <Field label="entrypoint">{d.graph_ref ?? d.handler_ref ?? "—"}</Field>
          <Field label="version">
            v{d.version ?? "?"} · {d.spec_hash?.slice(0, 12)}
          </Field>
        </KeyValue>
      </Section>

      <Section title="ceilings">
        <KeyValue>
          <Field label="cost">{cents(d.ceilings.max_cost_cents ?? 0)}</Field>
          <Field label="llm calls">{d.ceilings.max_llm_calls ?? "—"}</Field>
          <Field label="tool calls">{d.ceilings.max_tool_calls ?? "—"}</Field>
          <Field label="wall clock">
            {d.ceilings.max_wall_clock_s ? `${d.ceilings.max_wall_clock_s}s` : "—"}
          </Field>
          <Field label="depth">{d.ceilings.max_depth ?? "—"}</Field>
        </KeyValue>
      </Section>

      <Section title={`tools (${d.tools.length})`}>
        {d.tools.length ? (
          <BadgeRow>
            {d.tools.map((tool) => (
              <Chip key={tool}>{tool}</Chip>
            ))}
          </BadgeRow>
        ) : (
          <Empty>No tool grants — this actor cannot reach the outside.</Empty>
        )}
      </Section>

      {Object.keys(d.model_profiles).length > 0 && (
        <Section title="model profiles">
          <Fields rows={Object.entries(d.model_profiles)} />
        </Section>
      )}

      {d.triggers.length > 0 && (
        <Section title="schedule">
          <Fields
            rows={d.triggers.map((trigger) => [
              trigger.key,
              `${trigger.cron} ${trigger.timezone}${trigger.active ? "" : " (off)"}`,
            ])}
          />
        </Section>
      )}

      <RunControl
        orgId={context.orgId}
        actor={node.label}
        modes={d.modes}
        onOpenRun={context.onOpenRun}
        onStarted={context.onRefresh}
      />

      <Section title="throughput">
        <KeyValue>
          <Field label="runs">
            {d.runs.success} ok / {d.runs.failed} failed / {d.runs.total} total
          </Field>
          <Field label="tasks">
            {d.tasks.accepted} accepted / {d.tasks.rejected} rejected / {d.tasks.open} open
          </Field>
          <Field label="spend">{cents(d.spend_cents)}</Field>
          <Field label="last run">{relative(d.last_run_at)}</Field>
        </KeyValue>
      </Section>

      {mine.length > 0 && (
        <Section title={`assigned tasks (${mine.length})`}>
          <List>
            {mine.slice(0, 8).map((task) => (
              <ListRow
                key={task.id}
                title={task.title}
                aside={<StatusTag status={task.outcome ?? task.status} />}
                meta={`${relative(task.created_at)}${task.rework_count > 0 ? ` · ${task.rework_count} rework` : ""}`}
              />
            ))}
          </List>
        </Section>
      )}

      <Section title="recent runs">
        {error && <Empty>{error}</Empty>}
        {loading && <Loading />}
        {detail?.runs.length === 0 && <Empty>This actor has never run.</Empty>}
        <List>
          {detail?.runs.map((run) => (
            <RunRow key={run.id} run={run} onOpen={context.onOpenRun} />
          ))}
        </List>
      </Section>
    </>
  );
}
