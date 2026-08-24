"use client";

import { Empty, Field, InlineButton, KeyValue, Section } from "@/components/ui";
import type { DepartmentNodeData } from "@/lib/types";
import type { PanelProps } from "../types";

export function DepartmentPanel({ node, context }: PanelProps) {
  const d = node.data as DepartmentNodeData;
  return (
    <Section title="department">
      <KeyValue>
        <Field label="head">
          {d.head ? (
            <InlineButton onClick={() => context.onFocusActor(d.head!)}>{d.head}</InlineButton>
          ) : (
            "none"
          )}
        </Field>
        <Field label="members">{d.members}</Field>
        <Field label="live runs">{d.runs_active}</Field>
      </KeyValue>
      <Empty>{d.description || "No description."}</Empty>
    </Section>
  );
}
