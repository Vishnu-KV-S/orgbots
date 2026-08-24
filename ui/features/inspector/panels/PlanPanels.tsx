"use client";

import { Field, KeyValue, Prose, Section } from "@/components/ui";
import type { GoalNodeData, ProjectNodeData } from "@/lib/types";
import type { PanelProps } from "../types";

export function GoalPanel({ node }: PanelProps) {
  const d = node.data as GoalNodeData;
  return (
    <Section title={`goal · ${d.horizon}`}>
      <Prose>{d.statement}</Prose>
      <KeyValue>
        <Field label="active">{String(d.active)}</Field>
      </KeyValue>
    </Section>
  );
}

export function ProjectPanel({ node }: PanelProps) {
  const d = node.data as ProjectNodeData;
  return (
    <Section title="project">
      <Prose>{d.description || "—"}</Prose>
      <KeyValue>
        <Field label="owner">{d.owner ?? "unowned"}</Field>
        <Field label="active">{String(d.active)}</Field>
      </KeyValue>
    </Section>
  );
}
