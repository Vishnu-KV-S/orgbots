"use client";

import { useCallback, useState } from "react";
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Fields,
  InlineButton,
  KeyValue,
  Section,
} from "@/components/ui";
import { startDepartment, stopDepartment, tickOrganization } from "@/lib/api";
import { useAction } from "@/lib/hooks/useAction";
import type { DepartmentNodeData } from "@/lib/types";
import type { PanelProps } from "../types";

/**
 * A department, and the three things an operator does to one: start it, stop it,
 * or crank it by hand.
 *
 * **Stop asks a question before it acts**, and the question is `drain` or `halt`.
 * That is not ceremony: `halt` refuses a call *after* its effect has fired, which
 * leaves an INTENT row in the journal for somebody to reconcile by hand. It is
 * the right trade when an effect in flight is worse than an effect you have to
 * reconcile — a runaway publish loop, a compromised credential — and the wrong
 * default for everything else, so it must be chosen rather than defaulted into.
 *
 * The reason is required for the same reason the CLI takes one: an engaged switch
 * with no reason is a thing the next person finds and does not dare disengage.
 */

const PROPAGATION_NOTE =
  "A worker can take up to ten seconds to see this: the kill-switch cache is " +
  "per-process, and disengaging it here clears only this process's copy.";

export function DepartmentPanel({ node, context }: PanelProps) {
  const d = node.data as DepartmentNodeData;
  const [confirming, setConfirming] = useState(false);
  const [mode, setMode] = useState<"drain" | "halt">("drain");
  const [reason, setReason] = useState("");

  const stop = useAction(
    useCallback(
      () => stopDepartment(context.orgId, node.label, mode, reason || "stopped from the UI"),
      [context.orgId, mode, node.label, reason],
    ),
    {
      onDone: () => {
        setConfirming(false);
        setReason("");
        context.onRefresh();
      },
    },
  );

  const start = useAction(
    useCallback(
      () => startDepartment(context.orgId, node.label, "started from the UI"),
      [context.orgId, node.label],
    ),
    { onDone: context.onRefresh },
  );

  const tick = useAction(
    useCallback(() => tickOrganization(context.orgId), [context.orgId]),
    { onDone: context.onRefresh },
  );

  const stopped = d.state === "stopped";

  return (
    <>
      <Section title="department">
        <KeyValue>
          <Field label="state">
            <span className={`state-tag ${d.state}`}>{d.state}</span>
          </Field>
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

      {d.kill_switch && (
        <Section title="stopped by">
          <KeyValue>
            <Field label="scope">
              {d.kill_switch.scope_type}
              {d.kill_switch.scope_id ? `:${d.kill_switch.scope_id}` : ""}
            </Field>
            <Field label="mode">{d.kill_switch.mode}</Field>
            <Field label="reason">{d.kill_switch.reason || "—"}</Field>
            <Field label="by">{d.kill_switch.engaged_by || "—"}</Field>
          </KeyValue>
          {d.kill_switch.scope_type === "org" && (
            <Empty>
              This is an <strong>organization-wide</strong> switch. Starting this
              department will not lift it.
            </Empty>
          )}
        </Section>
      )}

      <Section title={`schedule (${d.triggers.length})`}>
        {d.triggers.length ? (
          <Fields
            rows={d.triggers.map((trigger) => [
              trigger.key,
              `${trigger.cron} ${trigger.timezone} · ${trigger.actor}${
                trigger.active ? "" : " · paused"
              }`,
            ])}
          />
        ) : (
          <Empty>No triggers. Nothing will wake this department on its own.</Empty>
        )}
      </Section>

      <Section title="controls">
        <div className="action-row">
          {stopped ? (
            <Button onClick={() => void start.run()} title="Disengage the switch and resume the triggers">
              {start.pending ? "starting…" : "start"}
            </Button>
          ) : (
            <Button onClick={() => setConfirming((on) => !on)} title="Engage a kill switch and pause the triggers">
              stop…
            </Button>
          )}
          <Button
            onClick={() => void tick.run()}
            title="Evaluate this organization's schedule now. Creates runs; executes none."
          >
            {tick.pending ? "ticking…" : "tick now"}
          </Button>
        </div>

        {confirming && !stopped && (
          <div className="confirm">
            <label className="field-row">
              <span>mode</span>
              <select
                className="input"
                value={mode}
                onChange={(event) => setMode(event.target.value as "drain" | "halt")}
              >
                <option value="drain">drain — let in-flight calls finish</option>
                <option value="halt">halt — refuse after the effect too</option>
              </select>
            </label>
            <label className="field-row">
              <span>reason</span>
              <input
                className="input"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="why — the next person reads this"
              />
            </label>
            <p className="hint">
              {mode === "halt"
                ? "halt leaves an INTENT row in the effect journal for every call it " +
                  "catches mid-flight. Somebody reconciles those by hand."
                : "drain refuses new runs and new tool calls; a call already past the " +
                  "journal completes and commits."}
            </p>
            <div className="action-row">
              <Button onClick={() => void stop.run()} title="Engage it">
                {stop.pending ? "stopping…" : `stop ${node.label}`}
              </Button>
              <Button onClick={() => setConfirming(false)} title="Leave it running">
                cancel
              </Button>
            </div>
          </div>
        )}

        {stop.error && <ErrorNotice>{stop.error}</ErrorNotice>}
        {start.error && <ErrorNotice>{start.error}</ErrorNotice>}
        {tick.error && <ErrorNotice>{tick.error}</ErrorNotice>}

        {stop.result && <p className="ok-note">{PROPAGATION_NOTE}</p>}
        {start.result && (
          <p className="ok-note">
            {start.result.triggers_resumed} trigger(s) resumed. {PROPAGATION_NOTE}
          </p>
        )}
        {tick.result && (
          <p className="ok-note">
            {tick.result.fired.filter((f) => !f.skipped).length} fired,{" "}
            {tick.result.dispatched} dispatched — queued for a worker.
          </p>
        )}
      </Section>
    </>
  );
}
