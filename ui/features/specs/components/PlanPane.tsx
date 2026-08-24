"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Button, Empty, ErrorNotice, Section } from "@/components/ui";
import { applySpec, planSpec, validateSpec, type PlanOptions } from "@/lib/api";
import { useAction } from "@/lib/hooks/useAction";
import type { PlanResult, SpecFolder } from "@/lib/types";

/**
 * validate → plan → read the diff → apply.
 *
 * **The three steps are three buttons on purpose.** §13's first risk is a quiet
 * bad apply, and the stated guard is to treat a plan diff like a code review —
 * which only works if reading it and applying it are separate acts with a hash
 * tying them together. Collapsing this into one *Apply* button would remove the
 * only place a person can notice that the diff deletes an actor.
 *
 * Two things this pane exists to make visible, because both are invisible in a
 * CLI transcript and expensive to discover as a 409:
 *
 * - **the expiry.** A plan is stale after fifteen minutes, and fifteen minutes is
 *   exactly how long reading a large diff takes. The countdown is here so the
 *   answer to "can I still apply this" is on screen rather than in a status code.
 * - **the rename.** `build_plan` refuses to turn an actor into a different actor
 *   without being told to — renaming is a delete plus a create, and the new one
 *   has no version history, no memory and no task history. The CLI answers with
 *   `--rename old=new`; without the equivalent here, renaming an actor in the
 *   editor is a dead end.
 */

const RENAME_HINT =
  "old=new, one per line. A rename is a delete plus a create: the new actor starts " +
  "with no version history, no memory and no task history.";

export interface PlanPaneProps {
  folder: SpecFolder;
  organizationId: string;
  onOrganizationId: (next: string) => void;
  /** Told after an apply, so the tree can re-read which folders are now applied. */
  onApplied: () => void;
}

export function PlanPane({
  folder,
  organizationId,
  onOrganizationId,
  onApplied,
}: PlanPaneProps) {
  const [plan, setPlan] = useState<PlanResult | null>(null);
  const [renamesText, setRenamesText] = useState("");
  const [allowReplace, setAllowReplace] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);

  // A new folder is a new diff. Keeping the old one on screen would let somebody
  // apply a plan they made against a different corpus.
  useEffect(() => {
    setPlan(null);
    setRenamesText("");
    setAllowReplace(false);
  }, [folder.path]);

  const options: PlanOptions = {
    renames: parseRenames(renamesText),
    allowReplace,
  };

  const validate = useAction(useCallback(() => validateSpec(folder.path), [folder.path]));
  const makePlan = useAction(
    useCallback(
      () => planSpec(organizationId, folder.path, options),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [organizationId, folder.path, renamesText, allowReplace],
    ),
    { onDone: setPlan },
  );
  const apply = useAction(
    useCallback(
      () => applySpec(organizationId, folder.path, plan?.plan_id ?? null, options),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [organizationId, folder.path, plan, renamesText, allowReplace],
    ),
    {
      onDone: () => {
        setPlan(null);
        onApplied();
      },
    },
  );

  // The countdown. A second is the right resolution: it is a fifteen-minute window
  // and the only moment that matters is when it reaches zero.
  useEffect(() => {
    if (!plan?.expires_at) {
      setRemaining(null);
      return;
    }
    const deadline = new Date(plan.expires_at).getTime();
    const tick = () => setRemaining(Math.max(0, Math.round((deadline - Date.now()) / 1000)));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [plan?.expires_at]);

  const expired = remaining !== null && remaining === 0;
  // `RenameRefused` is a 409 and it is the one the operator can answer from here.
  const refusedRename =
    makePlan.status === 409 && /rename/i.test(makePlan.error ?? "");

  return (
    <div className="plan-pane">
      <Section title="organization">
        <label className="field-row">
          <span>id</span>
          <input
            className="input mono"
            value={organizationId}
            onChange={(event) => onOrganizationId(event.target.value.trim())}
            spellCheck={false}
          />
        </label>
        <p className="hint">
          {folder.organization_id
            ? `${folder.organization ?? "this folder"} is already applied under this id.`
            : `No organization named ${folder.organization ?? "?"} exists yet — this id will
               create one. It is a fresh UUID; replace it to apply into an existing company.`}
        </p>
      </Section>

      <Section title="actions">
        <div className="action-row">
          <Button onClick={() => void validate.run()} title="Parse and check; touches no database">
            {validate.pending ? "validating…" : "validate"}
          </Button>
          <Button onClick={() => void makePlan.run()} title="The reviewable diff, with a hash">
            {makePlan.pending ? "planning…" : "plan"}
          </Button>
          <Button
            onClick={() => void apply.run()}
            title={
              plan
                ? "Apply the plan above, transactionally"
                : "Plan first — apply without one applies a diff nobody read"
            }
          >
            {apply.pending ? "applying…" : "apply"}
          </Button>
        </div>

        {validate.error && <ErrorNotice>{validate.error}</ErrorNotice>}
        {validate.result && (
          <p className="ok-note">
            {validate.result.documents} document(s) ok · {validate.result.organization} ·{" "}
            {validate.result.actors.length} actor(s) · fingerprint{" "}
            <code>{validate.result.fingerprint.slice(0, 12)}</code>
          </p>
        )}

        {makePlan.error && <ErrorNotice>{makePlan.error}</ErrorNotice>}
        {apply.error && <ErrorNotice>{apply.error}</ErrorNotice>}
        {apply.result?.applied && (
          <p className="ok-note">
            applied {apply.result.changed} change(s): {apply.result.created} created,{" "}
            {apply.result.updated} updated, {apply.result.deactivated} deactivated.{" "}
            <Link href={`/org/${organizationId}`}>open the canvas →</Link>
          </p>
        )}
        {apply.result && !apply.result.applied && <Empty>{apply.result.detail}</Empty>}
      </Section>

      {(refusedRename || renamesText || allowReplace) && (
        <Section title="renames">
          <textarea
            className="input mono textarea"
            rows={3}
            placeholder="old-name=new-name"
            value={renamesText}
            onChange={(event) => setRenamesText(event.target.value)}
            spellCheck={false}
          />
          <label className="check-row">
            <input
              type="checkbox"
              checked={allowReplace}
              onChange={(event) => setAllowReplace(event.target.checked)}
            />
            <span>
              allow replace — the actor I removed and the one I added are genuinely
              different actors
            </span>
          </label>
          <p className="hint">{RENAME_HINT}</p>
        </Section>
      )}

      {plan && (
        <Section
          title={
            <>
              plan{" "}
              <span className="mono dim">{plan.plan_hash.slice(0, 12)}</span>
              {remaining !== null && (
                <span className={expired ? "expiry gone" : "expiry"}>
                  {expired
                    ? " expired — re-plan"
                    : ` expires in ${Math.floor(remaining / 60)}m ${remaining % 60}s`}
                </span>
              )}
            </>
          }
        >
          {/* The CLI's own rendering, verbatim. Two renderings of one diff is two
              things that can disagree about what an apply will do. */}
          <pre className="plan-render">{plan.render}</pre>
          {plan.warnings.length > 0 && (
            <ul className="plan-warnings">
              {plan.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {!plan && !makePlan.pending && (
        <Empty>
          No plan yet. <strong>validate</strong> checks the documents without a database;{" "}
          <strong>plan</strong> diffs them against this organization and hashes the result;{" "}
          <strong>apply</strong> writes it in one transaction.
        </Empty>
      )}
    </div>
  );
}

/** `old=new` per line, ignoring blanks and anything without an `=`. */
function parseRenames(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const [old, next] = line.split("=");
    if (old?.trim() && next?.trim()) out[old.trim()] = next.trim();
  }
  return out;
}
