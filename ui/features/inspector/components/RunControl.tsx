"use client";

import { useCallback, useState } from "react";
import { Button, ErrorNotice, Section } from "@/components/ui";
import { fetchRun, startActorRun } from "@/lib/api";
import { useAction } from "@/lib/hooks/useAction";
import type { RunDetail } from "@/lib/types";

/**
 * Ask one actor to run.
 *
 * **The mode is a `<select>` when the entrypoint declared its modes and a text
 * box when it did not.** `modes` is read off the graph's own branch function, so
 * an empty list means *unknown*, not *none* — and a select with no options would
 * be a control that says "this actor cannot be run", which is a different and
 * false claim. `echo_agent@1` and `delegator@1` are the honest empty cases.
 *
 * The input is raw JSON on purpose. Every graph takes a different payload and
 * there is no schema for it — a form built from guesses would be wrong for the
 * actor somebody actually wants to poke.
 *
 * On success it opens the run in the inspector: a control that starts something
 * and then leaves you to find it is a control you use once.
 */

export interface RunControlProps {
  orgId: string;
  actor: string;
  modes: string[];
  onOpenRun: (run: RunDetail) => void;
  onStarted: () => void;
}

export function RunControl({ orgId, actor, modes, onOpenRun, onStarted }: RunControlProps) {
  const [mode, setMode] = useState(modes[0] ?? "");
  const [showInput, setShowInput] = useState(false);
  const [inputText, setInputText] = useState("{}");
  const [inputError, setInputError] = useState<string | null>(null);

  const start = useAction(
    useCallback(async () => {
      let input: Record<string, unknown> = {};
      if (showInput && inputText.trim()) {
        try {
          input = JSON.parse(inputText) as Record<string, unknown>;
        } catch (cause) {
          // Thrown rather than silently sending `{}`: a run started with the
          // payload you did not write is worse than a run that did not start.
          throw new Error(`input is not JSON: ${(cause as Error).message}`);
        }
      }
      const started = await startActorRun(orgId, actor, { mode, input });
      onStarted();
      // The run view wants the full detail, and the start response is a receipt.
      onOpenRun(await fetchRun(started.run_id));
      return started;
    }, [actor, inputText, mode, onOpenRun, onStarted, orgId, showInput]),
  );

  return (
    <Section title="run">
      <label className="field-row">
        <span>mode</span>
        {modes.length > 0 ? (
          <select
            className="input"
            value={mode}
            onChange={(event) => setMode(event.target.value)}
          >
            {modes.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="input mono"
            value={mode}
            placeholder="this entrypoint declares no modes"
            onChange={(event) => setMode(event.target.value)}
            spellCheck={false}
          />
        )}
      </label>

      <button
        type="button"
        className="linklike"
        onClick={() => setShowInput((on) => !on)}
      >
        {showInput ? "hide input" : "input JSON…"}
      </button>

      {showInput && (
        <>
          <textarea
            className="input mono textarea"
            rows={5}
            value={inputText}
            spellCheck={false}
            onChange={(event) => {
              setInputText(event.target.value);
              try {
                JSON.parse(event.target.value || "{}");
                setInputError(null);
              } catch (cause) {
                setInputError((cause as Error).message);
              }
            }}
          />
          {inputError && <p className="hint bad">{inputError}</p>}
        </>
      )}

      <div className="action-row">
        <Button
          onClick={() => void start.run()}
          title="Admitted through RunService: authority, budget and kill switch all apply"
        >
          {start.pending ? "starting…" : `run ${actor}`}
        </Button>
      </div>

      {start.error && <ErrorNotice>{start.error}</ErrorNotice>}
      {start.result && !start.result.created && (
        <p className="ok-note">
          That idempotency key already had a run — this is the existing one, not a
          second.
        </p>
      )}
    </Section>
  );
}
