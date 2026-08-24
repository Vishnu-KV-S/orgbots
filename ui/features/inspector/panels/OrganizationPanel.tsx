"use client";

import { Empty, Field, KeyValue, List, ListRow, Section, StatusTag } from "@/components/ui";
import { cents, relative } from "@/lib/format";
import type { OrganizationNodeData } from "@/lib/types";
import type { PanelProps } from "../types";

export function OrganizationPanel({ node }: PanelProps) {
  const d = node.data as OrganizationNodeData;
  return (
    <>
      <Section title="organization">
        <KeyValue>
          <Field label="id">{d.id}</Field>
          <Field label="created">{relative(d.created_at)}</Field>
          <Field label="departments">{d.departments}</Field>
          <Field label="actors">{d.actors}</Field>
          <Field label="live runs">{d.runs_active}</Field>
          <Field label="total spend">{cents(d.spend_cents)}</Field>
        </KeyValue>
      </Section>

      <Section title="kill switches">
        {d.kill_switches.length === 0 ? (
          <Empty>None engaged.</Empty>
        ) : (
          <List>
            {d.kill_switches.map((k, index) => (
              <ListRow
                key={`${k.scope_type}:${k.scope_id ?? index}`}
                title={`${k.scope_type} · ${k.mode}`}
                aside={<StatusTag status="FAILED">engaged</StatusTag>}
                meta={`${k.reason ?? "no reason given"} — ${k.engaged_by ?? "unknown"}, ${relative(k.engaged_at)}`}
              />
            ))}
          </List>
        )}
      </Section>
    </>
  );
}
