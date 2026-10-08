"use client";

import { useCallback, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type Routine,
  type RoutineDraft,
  type RoutineRun,
  createRoutine,
  deleteRoutine,
  listRoutineRuns,
  listRoutines,
  testRoutine,
  updateRoutine,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { timeAgo } from "../lib/text";

type Preset = "daily" | "weekdays" | "weekly" | "monthly" | "hourly" | "custom";

const PRESETS: [Preset, string][] = [
  ["daily", "Every day"],
  ["weekdays", "Weekdays"],
  ["weekly", "Every week"],
  ["monthly", "Every month"],
  ["hourly", "Every hour"],
  ["custom", "Custom (cron)"],
];

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const SOURCES = [
  ["github", "GitHub webhook"],
  ["slack", "Slack events"],
  ["webhook", "Any webhook (JSON)"],
] as const;

interface When {
  preset: Preset;
  time: string;
  day: number;
  date: number;
  minute: number;
  cron: string;
}

function localZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** The schedule controls → a 5-field cron. The server checks it (spacing, timezone). */
function cronOf(w: When): string {
  const [h, m] = w.time.split(":").map((n) => Number.parseInt(n, 10) || 0);
  switch (w.preset) {
    case "daily":
      return `${m} ${h} * * *`;
    case "weekdays":
      return `${m} ${h} * * 1-5`;
    case "weekly":
      return `${m} ${h} * * ${w.day}`;
    case "monthly":
      return `${m} ${h} ${w.date} * *`;
    case "hourly":
      return `${w.minute} * * * *`;
    default:
      return w.cron.trim();
  }
}

/** A saved cron back into the controls, when it is one of the shapes they make. */
function whenOf(cron: string | null): When {
  const base: When = { preset: "daily", time: "09:00", day: 1, date: 1, minute: 0, cron: "" };
  if (!cron) return base;
  const [mi, ho, dom, mon, dow] = cron.split(/\s+/);
  const time =
    /^\d+$/.test(mi) && /^\d+$/.test(ho)
      ? `${ho.padStart(2, "0")}:${mi.padStart(2, "0")}`
      : base.time;
  if (/^\d+$/.test(mi) && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { ...base, preset: "hourly", minute: Number(mi) };
  }
  if (/^\d+$/.test(mi) && /^\d+$/.test(ho) && mon === "*") {
    if (dom === "*" && dow === "*") return { ...base, preset: "daily", time };
    if (dom === "*" && dow === "1-5") return { ...base, preset: "weekdays", time };
    if (dom === "*" && /^\d$/.test(dow))
      return { ...base, preset: "weekly", time, day: Number(dow) };
    if (/^\d+$/.test(dom) && dow === "*")
      return { ...base, preset: "monthly", time, date: Number(dom) };
  }
  return { ...base, preset: "custom", cron };
}

const EMPTY: RoutineDraft = {
  name: "",
  instruction: "",
  kind: "schedule",
  cron: null,
  timezone: "UTC",
  source: null,
  match: { events: [], contains: "", actor: "" },
  inputs: "",
  output: "",
  approval: "default",
  when_missing: "",
  active: true,
};

function draftOf(r: Routine): RoutineDraft {
  return {
    name: r.name,
    instruction: r.instruction,
    kind: r.kind,
    cron: r.cron,
    timezone: r.timezone,
    source: r.source,
    match: {
      events: r.match.events ?? [],
      contains: r.match.contains ?? "",
      actor: r.match.actor ?? "",
    },
    inputs: r.inputs,
    output: r.output,
    approval: r.approval,
    when_missing: r.when_missing,
    active: r.active,
  };
}

function untilText(iso: string | null): string {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 60_000) return "any moment";
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `in ${hours} h`;
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

const RUN_LABEL: Record<RoutineRun["status"], string> = {
  queued: "Waiting",
  started: "Ran",
  refused: "Refused",
  skipped: "Skipped",
  missed: "Missed",
};

/**
 * Work this bot does by itself, on a schedule or when an event arrives.
 *
 * A routine's firing is a message in the conversation, so its result is there too.
 * Firings never interrupt: a busy bot (or one waiting on you) gets it when it is free,
 * and missed runs are recorded, not made up. "Test run" runs it now, drafts only.
 */
export function RoutinesSection({ bot }: { bot: Bot }) {
  const fetcher = useCallback((signal: AbortSignal) => listRoutines(bot.id, signal), [bot.id]);
  const routines = useResource(fetcher, { intervalMs: 8000 });
  const [editing, setEditing] = useState<Routine | "new" | null>(null);
  const [history, setHistory] = useState<string | null>(null);
  const [tested, setTested] = useState<string | null>(null);

  const toggle = useAction((r: Routine) => updateRoutine(bot.id, r.id, { active: !r.active }), {
    onDone: routines.refresh,
  });
  const remove = useAction((r: Routine) => deleteRoutine(bot.id, r.id), {
    onDone: routines.refresh,
  });
  const test = useAction((r: Routine) => testRoutine(bot.id, r.id), {
    onDone: (sent) => {
      setTested(
        sent.admitted
          ? "Test run started — watch the conversation."
          : (sent.refusal_reason ?? "Refused"),
      );
      routines.refresh();
    },
  });
  const list = routines.data?.routines ?? [];

  return (
    <section className="dsec">
      <h3>Routines</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        Work {bot.name} starts by itself, on a schedule or when an event arrives. You can also just
        ask in the chat: “every weekday at 8, check the support inbox”. A routine waits while{" "}
        {bot.name} is busy or waiting on you, and never makes up runs it missed.
      </p>

      {list.map((r) => (
        <div key={r.id} className="routine">
          <div className="routine-head">
            <span className={`decision ${r.active ? "ask" : ""}`}>
              {r.active ? "On" : "Paused"}
            </span>
            <strong className="grow">{r.name}</strong>
            <button
              type="button"
              className="ibtn"
              title="Run it now, drafts only"
              disabled={test.pending}
              onClick={() => void test.run(r)}
            >
              ▶
            </button>
            <button type="button" className="ibtn" title="Edit" onClick={() => setEditing(r)}>
              ✎
            </button>
            <button
              type="button"
              className="ibtn"
              aria-label={`Delete routine ${r.name}`}
              disabled={remove.pending}
              onClick={() => {
                if (window.confirm(`Delete the routine “${r.name}”?`)) void remove.run(r);
              }}
            >
              ✕
            </button>
          </div>
          <div className="routine-meta">
            {r.kind === "schedule"
              ? r.schedule
              : `When ${SOURCES.find(([k]) => k === r.source)?.[1] ?? "an event"} sends ${
                  r.match.events?.length ? r.match.events.join(", ") : "anything"
                }`}
            {r.active && r.next_fire_at && <> · next {untilText(r.next_fire_at)}</>}
            {r.approval === "drafts" && <> · drafts only</>}
            {r.created_by_kind === "bot" && <> · set up by {bot.name}</>}
          </div>
          <div className="routine-what">{r.instruction}</div>
          <div className="routine-meta">
            {r.last_run ? (
              <button
                type="button"
                className="linklike"
                onClick={() => setHistory(history === r.id ? null : r.id)}
              >
                {RUN_LABEL[r.last_run.status]} {timeAgo(r.last_run.created_at)}
                {r.last_run.detail ? ` — ${r.last_run.detail}` : ""} · history
              </button>
            ) : (
              "Not run yet"
            )}
            {" · "}
            <button type="button" className="linklike" onClick={() => void toggle.run(r)}>
              {r.active ? "Pause" : "Resume"}
            </button>
          </div>
          {r.hook_url && <HookUrl url={r.hook_url} signed={r.has_secret} source={r.source} />}
          {history === r.id && <History bot={bot} routine={r} />}
        </div>
      ))}
      {routines.data && list.length === 0 && editing === null && (
        <p className="screen-help">No routines yet.</p>
      )}
      {tested && <p className="screen-help">{tested}</p>}
      {routines.error && <ErrorNotice>{routines.error}</ErrorNotice>}
      {(toggle.error || remove.error || test.error) && (
        <ErrorNotice>{toggle.error || remove.error || test.error}</ErrorNotice>
      )}

      {editing ? (
        <RoutineForm
          bot={bot}
          routine={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            routines.refresh();
          }}
        />
      ) : (
        <div className="form-actions" style={{ marginTop: 10 }}>
          <button type="button" className="pbtn" onClick={() => setEditing("new")}>
            + New routine
          </button>
        </div>
      )}
    </section>
  );
}

function HookUrl({ url, signed, source }: { url: string; signed: boolean; source: string | null }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="routine-hook">
      <code title={url}>{url}</code>
      <button
        type="button"
        className="ibtn"
        onClick={() => {
          void navigator.clipboard?.writeText(url);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? "✓" : "Copy"}
      </button>
      <div className="routine-meta">
        Paste this into{" "}
        {source === "github"
          ? "the repository's Webhooks settings"
          : source === "slack"
            ? "your Slack app's Event Subscriptions"
            : "the sending service"}
        .{" "}
        {signed
          ? "Signatures are checked."
          : "Add a signing secret so only the real sender can start it."}
      </div>
    </div>
  );
}

function History({ bot, routine }: { bot: Bot; routine: Routine }) {
  const fetcher = useCallback(
    (signal: AbortSignal) => listRoutineRuns(bot.id, routine.id, signal),
    [bot.id, routine.id],
  );
  const runs = useResource(fetcher, { intervalMs: 8000 });
  return (
    <div className="routine-runs">
      {runs.data?.runs.map((run) => (
        <div key={run.id} className="routine-run">
          <span className={`run-dot ${run.status}`} />
          <span className="grow">
            {RUN_LABEL[run.status]}
            {run.trigger === "test"
              ? " (test)"
              : run.trigger === "event"
                ? ` · ${run.event.name ?? "event"}`
                : ""}
            {run.detail ? ` — ${run.detail}` : ""}
          </span>
          <span className="muted">
            {new Date(run.scheduled_for ?? run.created_at).toLocaleString()}
          </span>
        </div>
      ))}
      {runs.data?.runs.length === 0 && <p className="screen-help">No runs yet.</p>}
    </div>
  );
}

function RoutineForm({
  bot,
  routine,
  onClose,
  onSaved,
}: {
  bot: Bot;
  routine: Routine | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<RoutineDraft>(() =>
    routine ? draftOf(routine) : { ...EMPTY, timezone: localZone() },
  );
  const [when, setWhen] = useState<When>(() => whenOf(routine?.cron ?? null));
  const [events, setEvents] = useState(() => (routine?.match.events ?? []).join(", "));
  const [secret, setSecret] = useState("");
  const set = (fields: Partial<RoutineDraft>) => setDraft((d) => ({ ...d, ...fields }));

  const save = useAction(
    () => {
      const body: RoutineDraft = {
        ...draft,
        cron: draft.kind === "schedule" ? cronOf(when) : null,
        source: draft.kind === "event" ? (draft.source ?? "webhook") : null,
        match: {
          ...draft.match,
          events: events
            .split(",")
            .map((e) => e.trim())
            .filter(Boolean),
        },
        ...(secret ? { signing_secret: secret } : {}),
      };
      return routine ? updateRoutine(bot.id, routine.id, body) : createRoutine(bot.id, body);
    },
    { onDone: onSaved },
  );

  return (
    <div className="form routine-form">
      <div className="form-row">
        <label>
          Name
          <input
            value={draft.name}
            placeholder="Morning inbox sweep"
            onChange={(e) => set({ name: e.target.value })}
          />
        </label>
        {!routine && (
          <label style={{ flex: "none" }}>
            Starts
            <select
              value={draft.kind}
              onChange={(e) =>
                set({
                  kind: e.target.value as RoutineDraft["kind"],
                  source: e.target.value === "event" ? "github" : null,
                })
              }
            >
              <option value="schedule">On a schedule</option>
              <option value="event">When an event arrives</option>
            </select>
          </label>
        )}
      </div>

      {draft.kind === "schedule" ? (
        <>
          <div className="form-row">
            <label>
              When
              <select
                value={when.preset}
                onChange={(e) => setWhen({ ...when, preset: e.target.value as Preset })}
              >
                {PRESETS.map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {when.preset === "weekly" && (
              <label>
                On
                <select
                  value={when.day}
                  onChange={(e) => setWhen({ ...when, day: Number(e.target.value) })}
                >
                  {DAYS.map((d, i) => (
                    <option key={d} value={i}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {when.preset === "monthly" && (
              <label>
                Day
                <input
                  type="number"
                  min={1}
                  max={28}
                  value={when.date}
                  onChange={(e) => setWhen({ ...when, date: Number(e.target.value) || 1 })}
                />
              </label>
            )}
            {when.preset === "hourly" ? (
              <label>
                At minute
                <input
                  type="number"
                  min={0}
                  max={59}
                  value={when.minute}
                  onChange={(e) => setWhen({ ...when, minute: Number(e.target.value) || 0 })}
                />
              </label>
            ) : when.preset === "custom" ? (
              <label>
                <span>
                  Cron <small>— min hour day month weekday</small>
                </span>
                <input
                  value={when.cron}
                  placeholder="0 8 * * 1-5"
                  onChange={(e) => setWhen({ ...when, cron: e.target.value })}
                />
              </label>
            ) : (
              <label>
                At
                <input
                  type="time"
                  value={when.time}
                  onChange={(e) => setWhen({ ...when, time: e.target.value })}
                />
              </label>
            )}
          </div>
          <label>
            Timezone
            <input value={draft.timezone} onChange={(e) => set({ timezone: e.target.value })} />
          </label>
        </>
      ) : (
        <>
          <div className="form-row">
            <label>
              From
              <select
                value={draft.source ?? "github"}
                onChange={(e) => set({ source: e.target.value as RoutineDraft["source"] })}
                disabled={routine !== null}
              >
                {SOURCES.map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>
                Events <small>— comma-separated, blank for all</small>
              </span>
              <input
                value={events}
                placeholder={
                  draft.source === "slack" ? "app_mention, message" : "issues.opened, pull_request"
                }
                onChange={(e) => setEvents(e.target.value)}
              />
            </label>
          </div>
          <div className="form-row">
            <label>
              <span>
                Containing <small>— optional</small>
              </span>
              <input
                value={draft.match.contains}
                onChange={(e) => set({ match: { ...draft.match, contains: e.target.value } })}
              />
            </label>
            <label>
              <span>
                From user <small>— optional</small>
              </span>
              <input
                value={draft.match.actor}
                onChange={(e) => set({ match: { ...draft.match, actor: e.target.value } })}
              />
            </label>
          </div>
          <label>
            <span>
              Signing secret{" "}
              <small>
                — {routine?.has_secret ? "set; type a new one to replace it" : "recommended"}
              </small>
            </span>
            <input
              type="password"
              autoComplete="off"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
          </label>
        </>
      )}

      <label>
        What to do each time
        <textarea
          rows={3}
          value={draft.instruction}
          placeholder="Check the support inbox and summarise anything urgent, with links."
          onChange={(e) => set({ instruction: e.target.value })}
        />
      </label>
      <div className="form-row">
        <label>
          <span>
            Input <small>— where it comes from</small>
          </span>
          <input
            value={draft.inputs}
            placeholder="support@ inbox in Gmail"
            onChange={(e) => set({ inputs: e.target.value })}
          />
        </label>
        <label>
          <span>
            Deliver <small>— what, and where</small>
          </span>
          <input
            value={draft.output}
            placeholder="A summary in the chat"
            onChange={(e) => set({ output: e.target.value })}
          />
        </label>
      </div>
      <div className="form-row">
        <label>
          If the input is missing
          <input
            value={draft.when_missing}
            placeholder="Say so in one line and stop"
            onChange={(e) => set({ when_missing: e.target.value })}
          />
        </label>
        <label style={{ flex: "none" }}>
          Approval
          <select
            value={draft.approval}
            onChange={(e) => set({ approval: e.target.value as RoutineDraft["approval"] })}
          >
            <option value="default">Use the bot&apos;s rules</option>
            <option value="drafts">Drafts only — never send</option>
          </select>
        </label>
      </div>
      {save.error && <ErrorNotice>{save.error}</ErrorNotice>}
      <div className="form-actions">
        <button type="button" className="pbtn" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={save.pending || !draft.name.trim() || !draft.instruction.trim()}
          onClick={() => void save.run()}
        >
          {routine ? "Save routine" : "Create routine"}
        </button>
      </div>
    </div>
  );
}
