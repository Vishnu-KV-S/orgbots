"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotBrief,
  type BriefRevision,
  briefOf,
  listRevisions,
  restoreRevision,
  updateBot,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { timeAgo } from "../lib/text";

/** Lines are edited as text so a new, still-empty line survives typing; the server
 * drops blank lines and duplicates when it saves. */
function Lines({
  value,
  onChange,
  placeholder,
  rows = 3,
}: {
  value: string[];
  onChange: (lines: string[]) => void;
  placeholder?: string;
  rows?: number;
}) {
  return (
    <textarea
      rows={Math.max(rows, value.length + 1)}
      value={value.join("\n")}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value.split("\n"))}
    />
  );
}

/**
 * The fields of a brief. `compact` is the new-bot dialog: the mission and the lines it
 * must not cross, which is what a person knows on day one. The rest can come later.
 */
export function BriefFields({
  value,
  onChange,
  compact = false,
}: {
  value: BotBrief;
  onChange: (brief: BotBrief) => void;
  compact?: boolean;
}) {
  const set = <K extends keyof BotBrief>(key: K, v: BotBrief[K]) =>
    onChange({ ...value, [key]: v });
  return (
    <>
      <label>
        <span>
          Mission <small>— the job, in a sentence or two</small>
        </span>
        <textarea
          rows={2}
          value={value.mission}
          placeholder="e.g. Keep my travel organised: find, compare and hold bookings."
          onChange={(e) => set("mission", e.target.value)}
        />
      </label>
      {!compact && (
        <label>
          <span>
            Responsibilities <small>— one per line</small>
          </span>
          <Lines
            value={value.duties}
            onChange={(v) => set("duties", v)}
            placeholder={"e.g. Find flights and hotels\ne.g. Keep a list of confirmed bookings"}
          />
        </label>
      )}
      <label>
        <span>
          Boundaries <small>— lines it must never cross, one per line</small>
        </span>
        <Lines
          value={value.boundaries}
          onChange={(v) => set("boundaries", v)}
          rows={compact ? 2 : 3}
          placeholder="e.g. Never pay for anything without asking me"
        />
      </label>
      {!compact && (
        <>
          <div className="form-row">
            <label>
              Working style
              <textarea
                rows={2}
                value={value.style}
                placeholder="e.g. Short answers, lead with the result"
                onChange={(e) => set("style", e.target.value)}
              />
            </label>
            <label>
              When to ask
              <textarea
                rows={2}
                value={value.escalation}
                placeholder="e.g. Before spending over $100"
                onChange={(e) => set("escalation", e.target.value)}
              />
            </label>
          </div>
          <label>
            Standing notes
            <textarea rows={3} value={value.notes} onChange={(e) => set("notes", e.target.value)} />
          </label>
        </>
      )}
    </>
  );
}

const clean = (b: BotBrief): BotBrief => ({
  ...b,
  duties: b.duties.map((l) => l.trim()).filter(Boolean),
  boundaries: b.boundaries.map((l) => l.trim()).filter(Boolean),
});

const same = (a: BotBrief, b: BotBrief) => JSON.stringify(clean(a)) === JSON.stringify(clean(b));

export function editorLabel(r: BriefRevision, bots: Bot[]): string {
  if (r.editor_kind === "person") return "You";
  if (r.editor_kind === "self") return `${r.editor_name || "The bot"} (itself)`;
  const parent = bots.find((b) => b.id === r.editor_bot_id);
  return `${parent?.name ?? r.editor_name ?? "Its parent bot"} (parent)`;
}

/**
 * The primary instruction, in the details pane. The bot reads it at the top of every
 * step; the bot itself and the bot that created it may revise it, and every revision —
 * theirs or yours — is listed here with who made it and why, and can be restored.
 */
export function BriefSection({
  bot,
  bots,
  onChanged,
}: {
  bot: Bot;
  bots: Bot[];
  onChanged: () => void;
}) {
  const saved = briefOf(bot);
  const [form, setForm] = useState<BotBrief>(saved);
  const [showHistory, setShowHistory] = useState(false);
  const dirty = !same(form, saved);
  // Follow the server while not editing, so a revision the bot makes appears live; a
  // different bot always replaces the form.
  const shownFor = useRef(bot.id);
  useEffect(() => {
    if (shownFor.current !== bot.id || !dirty) setForm(briefOf(bot));
    shownFor.current = bot.id;
  }, [bot.id, bot.brief_rev]); // eslint-disable-line react-hooks/exhaustive-deps

  const fetcher = useCallback(
    (signal: AbortSignal) => listRevisions(bot.id, signal),
    [bot.id, bot.brief_rev], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const revisions = useResource(fetcher, { intervalMs: 15000 });
  const latest = revisions.data?.revisions[0];

  const save = useAction(() => updateBot(bot.id, { brief: clean(form) }), { onDone: onChanged });
  const lock = useAction((locked: boolean) => updateBot(bot.id, { brief_locked: locked }), {
    onDone: onChanged,
  });
  const restore = useAction((rev: number) => restoreRevision(bot.id, rev), {
    onDone: () => {
      onChanged();
      revisions.refresh();
    },
  });

  return (
    <section className="dsec">
      <h3>Primary instruction</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        {bot.name}&apos;s job brief. It reads this before every step and it outranks everything else
        you tell it. {bot.name}
        {bot.parent_bot_id ? " and the bot that created it" : ""} can revise it as the job changes —
        every change is kept below.
      </p>
      <div className="form">
        <BriefFields value={form} onChange={setForm} />
        <label className="checkline">
          <input
            type="checkbox"
            checked={bot.brief_locked}
            disabled={lock.pending}
            onChange={(e) => void lock.run(e.target.checked)}
          />
          Only I can change this brief
        </label>
        {(save.error || lock.error || restore.error) && (
          <ErrorNotice>{save.error || lock.error || restore.error}</ErrorNotice>
        )}
        <div className="form-actions">
          {latest && (
            <span className="note">
              Rev {latest.rev} · {editorLabel(latest, bots)} · {timeAgo(latest.created_at)}
            </span>
          )}
          <button
            type="button"
            className="pbtn"
            disabled={!dirty || save.pending}
            onClick={() => setForm(saved)}
          >
            Reset
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={!dirty || save.pending}
            onClick={() => void save.run()}
          >
            Save brief
          </button>
        </div>
      </div>

      {(revisions.data?.revisions.length ?? 0) > 0 && (
        <div className="history">
          <button
            type="button"
            className="linklike"
            aria-expanded={showHistory}
            onClick={() => setShowHistory((s) => !s)}
          >
            {showHistory ? "Hide" : "Show"} history ({revisions.data?.revisions.length})
          </button>
          {showHistory && (
            <ol className="revisions">
              {revisions.data?.revisions.map((r) => (
                <li key={r.rev}>
                  <div className="rev-head">
                    <strong>Rev {r.rev}</strong>
                    <span>{editorLabel(r, bots)}</span>
                    <span className="muted">{timeAgo(r.created_at)}</span>
                    {r.rev !== bot.brief_rev && (
                      <button
                        type="button"
                        className="linklike"
                        disabled={restore.pending}
                        onClick={() => void restore.run(r.rev)}
                      >
                        Restore
                      </button>
                    )}
                  </div>
                  {r.changed.length > 0 && (
                    <div className="muted">Changed: {r.changed.join(", ")}</div>
                  )}
                  {r.reason && <div className="why">“{r.reason}”</div>}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}
