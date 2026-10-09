"use client";

import { XMarkIcon } from "@heroicons/react/24/outline";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotDraft,
  type ComputerStatus,
  type SearchResult,
  type TemplatePreview,
  createBot,
  resetComputer,
  searchBots,
  pushTest,
  sharedTemplate,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { cx } from "@/lib/cx";
import { Designer, randomAppearance } from "../avatar";
import { useMe } from "../lib/me";
import { type PushState, disablePush, enablePush, pushState } from "../lib/push";
import { TEMPLATES } from "../lib/templates";
import { Avatar } from "./Avatar";
import { BriefFields } from "./BriefEditor";
import { TemplateReview, readTemplateFile } from "./Sharing";
import { XSetting } from "./XSetting";

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal aria-label={title}>
        <div className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="ibtn" onClick={onClose} aria-label="Close">
            <XMarkIcon />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// --- new bot ------------------------------------------------------------------------

export function NewBotDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (bot: Bot) => void;
}) {
  const [form, setForm] = useState<BotDraft | null>(null);
  const [imported, setImported] = useState<TemplatePreview | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const create = useAction((draft: BotDraft) => createBot(draft), {
    onDone: (bot) => onCreated(bot),
  });
  const read = useAction((file: File) => readTemplateFile(file), { onDone: setImported });

  if (imported) {
    return (
      <Modal title="Create a bot from a template" onClose={onClose}>
        <TemplateReview
          preview={imported}
          source={{ template: imported.template }}
          onCreated={onCreated}
          onCancel={() => setImported(null)}
          cancelLabel="Back"
        />
      </Modal>
    );
  }

  if (!form) {
    return (
      <Modal title="Create a new bot" onClose={onClose}>
        <p className="screen-help" style={{ marginTop: 0 }}>
          A bot is a persistent AI employee: give it a role, and it remembers your preferences and
          its previous work. Pick a starting point — everything is editable later.
        </p>
        <div className="templates">
          {TEMPLATES.map((t) => (
            <button
              key={t.name}
              type="button"
              className="template"
              onClick={() =>
                setForm({
                  name: t.name,
                  label: t.label,
                  description: t.description,
                  brief: t.brief,
                  avatar: t.avatar,
                  appearance: t.name === "Assistant" ? randomAppearance() : t.appearance,
                })
              }
            >
              <Avatar appearance={t.appearance} size={34} />
              <strong>{t.name}</strong>
              <span>{t.blurb}</span>
            </button>
          ))}
        </div>
        <div className="control-row" style={{ marginTop: 12 }}>
          <button
            type="button"
            className="pbtn"
            disabled={read.pending}
            onClick={() => picker.current?.click()}
          >
            {read.pending ? "Reading…" : "Import a template file…"}
          </button>
          <input
            ref={picker}
            type="file"
            accept=".json,application/json"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void read.run(file);
            }}
          />
        </div>
        {read.error && <ErrorNotice>{read.error}</ErrorNotice>}
      </Modal>
    );
  }

  return (
    <Modal title="Create a new bot" onClose={onClose}>
      <div className="form">
        <Designer
          value={form.appearance ?? randomAppearance()}
          onChange={(appearance) => setForm({ ...form, appearance })}
          standalone
          previewSize={190}
        />
        <div className="form-row">
          <label>
            Name
            <input
              autoFocus
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label>
            Label
            <input
              value={form.label}
              placeholder="e.g. Research"
              onChange={(e) => setForm({ ...form, label: e.target.value })}
            />
          </label>
        </div>
        <label>
          <span>
            Description <small>— one line, shown in the sidebar</small>
          </span>
          <input
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        <BriefFields value={form.brief} onChange={(brief) => setForm({ ...form, brief })} compact />
        <p className="screen-help" style={{ margin: 0 }}>
          This is its primary instruction. Add responsibilities, working style and when to ask in
          its details later — and it can keep the brief up to date itself as the job changes.
        </p>
        {create.error && <ErrorNotice>{create.error}</ErrorNotice>}
        <div className="form-actions">
          <button type="button" className="pbtn" onClick={() => setForm(null)}>
            Back
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={!form.name.trim() || create.pending}
            onClick={() =>
              void create.run({
                ...form,
                name: form.name.trim(),
                brief: {
                  ...form.brief,
                  duties: form.brief.duties.map((l) => l.trim()).filter(Boolean),
                  boundaries: form.brief.boundaries.map((l) => l.trim()).filter(Boolean),
                },
              })
            }
          >
            {create.pending ? "Creating…" : "Create bot"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// --- command palette ----------------------------------------------------------------

interface PaletteEntry {
  key: string;
  label: string;
  hint?: string;
  bot?: Bot;
  run: () => void;
}

export function CommandPalette({
  bots,
  onClose,
  onSelectBot,
  onNew,
  onSettings,
}: {
  bots: Bot[];
  onClose: () => void;
  onSelectBot: (id: string) => void;
  onNew: () => void;
  onSettings: () => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult | null>(null);
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);

  useEffect(() => {
    if (q.trim().length < 2) {
      setResults(null);
      return;
    }
    const controller = new AbortController();
    const t = setTimeout(() => {
      searchBots(q.trim(), controller.signal).then(setResults, () => undefined);
    }, 180);
    return () => {
      clearTimeout(t);
      controller.abort();
    };
  }, [q]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const needle = q.toLowerCase();
    const commands: PaletteEntry[] = [
      { key: "new", label: "Create new bot", hint: "Command", run: onNew },
      {
        key: "settings",
        label: "Open settings",
        hint: "Command",
        run: onSettings,
      },
      {
        key: "companies",
        label: "Open companies console",
        hint: "Command",
        run: () => {
          window.location.href = "/companies";
        },
      },
    ].filter((c) => c.label.toLowerCase().includes(needle));
    const botEntries: PaletteEntry[] = bots
      .filter((b) => `${b.name} ${b.label}`.toLowerCase().includes(needle))
      .map((b) => ({
        key: `bot-${b.id}`,
        label: b.name,
        hint: b.label || "Bot",
        bot: b,
        run: () => onSelectBot(b.id),
      }));
    const messageEntries: PaletteEntry[] = (results?.messages ?? []).map((m) => ({
      key: `msg-${m.id}`,
      label: m.content
        .replace(/\*\*|`/g, "")
        .replace(/\s+/g, " ")
        .slice(0, 90),
      hint: m.bot_name,
      run: () => onSelectBot(m.bot_id),
    }));
    return [...botEntries, ...messageEntries, ...commands];
  }, [q, bots, results, onNew, onSettings, onSelectBot]);

  useEffect(() => setSel(0), [q]);

  const choose = (entry: PaletteEntry | undefined) => {
    if (!entry) return;
    onClose();
    entry.run();
  };

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="palette" role="dialog" aria-modal aria-label="Search">
        <input
          ref={input}
          value={q}
          placeholder="Search bots, messages and commands…"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSel((s) => Math.min(entries.length - 1, s + 1));
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setSel((s) => Math.max(0, s - 1));
            }
            if (e.key === "Enter") choose(entries[sel]);
          }}
        />
        <div className="palette-list" role="listbox">
          {entries.length === 0 && (
            <p className="screen-help" style={{ padding: 10 }}>
              No matches.
            </p>
          )}
          {entries.map((entry, i) => (
            <button
              key={entry.key}
              type="button"
              className={cx("popitem", i === sel && "sel")}
              onMouseEnter={() => setSel(i)}
              onClick={() => choose(entry)}
            >
              {entry.bot && <Avatar bot={entry.bot} size={22} />}
              <span
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {entry.label}
              </span>
              <small>{entry.hint}</small>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// --- settings -----------------------------------------------------------------------

export type Theme = "system" | "light" | "dark";

export function applyTheme(theme: Theme) {
  const resolved =
    theme === "system"
      ? window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark"
      : theme;
  document.documentElement.dataset.theme = resolved;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

function readSetting(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function notificationsEnabled(): boolean {
  return readSetting("notify", "off") === "on";
}

export function SettingsDialog({
  onClose,
  computer,
}: {
  onClose: () => void;
  computer: ComputerStatus | null;
}) {
  const [theme, setTheme] = useState<Theme>(() => readSetting("theme", "system") as Theme);
  const [notify, setNotify] = useState(() => readSetting("notify", "off") === "on");
  const reset = useAction(() => resetComputer());
  const me = useMe();

  return (
    <Modal title="Settings" onClose={onClose}>
      <section className="dsec">
        <h3>Appearance</h3>
        <div className="seg" role="radiogroup" aria-label="Theme">
          {(["system", "light", "dark"] as Theme[]).map((t) => (
            <button
              key={t}
              type="button"
              className={cx(theme === t && "on")}
              onClick={() => {
                setTheme(t);
                applyTheme(t);
              }}
            >
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
      </section>

      <section className="dsec">
        <h3>Notifications</h3>
        <label
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            fontSize: 13,
          }}
        >
          <input
            type="checkbox"
            checked={notify}
            onChange={async (e) => {
              const on = e.target.checked;
              if (on && "Notification" in window && Notification.permission === "default") {
                await Notification.requestPermission();
              }
              setNotify(on);
              try {
                localStorage.setItem("notify", on ? "on" : "off");
              } catch {
                /* ignore */
              }
            }}
          />
          Notify me when a bot replies or needs attention while this tab is in the background
        </label>
        <PushSetting />
      </section>

      <section className="dsec">
        <h3>Tag on X</h3>
        <XSetting />
      </section>

      <section className="dsec">
        <h3>Cloud computer</h3>
        <dl className="kv">
          <dt>Status</dt>
          <dd>{computer?.reachable ? "Running" : "Not reachable"}</dd>
          <dt>Address</dt>
          <dd>{computer?.url ?? "—"}</dd>
          <dt>Screens</dt>
          <dd>{computer?.screens?.length ?? 0} open</dd>
        </dl>
        <p className="screen-help">
          {me
            ? "Your bots share your own browser profile — your sign-ins and cookies, nobody else's — and a bot shared with the team has its own. Each bot has its own screen. Recover restarts everyone's browser (an admin's job); sign-ins survive."
            : "All your bots share one cloud computer — the same browser profile, sign-ins and cookies — and each has its own screen. Recover restarts the browser; sign-ins survive."}
        </p>
        {reset.error && <ErrorNotice>{reset.error}</ErrorNotice>}
        <div className="control-row">
          <button
            type="button"
            className="pbtn"
            disabled={reset.pending || !computer?.reachable}
            onClick={() => void reset.run()}
          >
            {reset.pending ? "Restarting…" : "Recover computer"}
          </button>
          {reset.result !== null && !reset.pending && (
            <span className="screen-help">Restarted.</span>
          )}
        </div>
      </section>

      <section className="dsec">
        <h3>Model</h3>
        <p className="screen-help" style={{ marginTop: 0 }}>
          Bots run on DeepSeek (<code>deepseek-v4-pro</code> for each step). Every run is admitted,
          budgeted and audited by the runtime like any company run.
        </p>
      </section>
    </Modal>
  );
}

// --- delete -------------------------------------------------------------------------

/** Every bot under `bot`, at any depth. */
export function helpersOf(bot: Bot, all: Bot[]): Bot[] {
  const out: Bot[] = [];
  const walk = (id: string) => {
    for (const b of all) {
      if (b.parent_bot_id === id && !out.includes(b)) {
        out.push(b);
        walk(b.id);
      }
    }
  };
  walk(bot.id);
  return out;
}

/**
 * Deleting a bot that has helpers is a choice, never a default: take the helpers
 * with it, or keep them — they move up a level and carry on with their memory,
 * conversations and routines intact.
 */
export function DeleteBotDialog({
  bot,
  bots,
  onClose,
  onDelete,
}: {
  bot: Bot;
  bots: Bot[];
  onClose: () => void;
  onDelete: (withHelpers: boolean) => Promise<void>;
}) {
  const helpers = helpersOf(bot, bots);
  const direct = helpers.filter((h) => h.parent_bot_id === bot.id);
  const parent = bots.find((b) => b.id === bot.parent_bot_id);
  // A team's files go with its last bot; helpers that stay keep them.
  const team = bots.filter((b) => b.team_id === bot.team_id);
  const doomed = new Set([bot.id, ...helpers.map((h) => h.id)]);
  const takesFilesAlone = team.every((b) => b.id === bot.id);
  const takesFilesWithHelpers = team.every((b) => doomed.has(b.id));
  const remove = useAction((withHelpers: boolean) => onDelete(withHelpers));
  const names = (list: Bot[]) =>
    list.length <= 3
      ? list.map((h) => h.name).join(", ")
      : `${list
          .slice(0, 3)
          .map((h) => h.name)
          .join(", ")} and ${list.length - 3} more`;

  return (
    <Modal title={`Delete ${bot.name}?`} onClose={onClose}>
      <p className="screen-help" style={{ marginTop: 0 }}>
        {bot.name}&apos;s conversation and memory will be deleted
        {takesFilesAlone && ", and so will the files its team shares"}. This cannot be undone.
        {helpers.length > 0 &&
          ` It has ${helpers.length} helper bot${helpers.length === 1 ? "" : "s"} under it — what should happen to ${helpers.length === 1 ? "it" : "them"}?`}
      </p>
      {helpers.length > 0 ? (
        <div className="choice-list">
          <button
            type="button"
            className="choice"
            disabled={remove.pending}
            onClick={() => void remove.run(false)}
          >
            <strong>Delete only {bot.name}</strong>
            <span>
              Keep {names(direct)} — {direct.length === 1 ? "it moves" : "they move"} up to{" "}
              {parent ? `${parent.name}` : "the top level"} and keep
              {direct.length === 1 ? "s" : ""} {direct.length === 1 ? "its" : "their"} memory and
              conversations, and the team&apos;s files.
            </span>
          </button>
          <button
            type="button"
            className="choice danger"
            disabled={remove.pending}
            onClick={() => void remove.run(true)}
          >
            <strong>
              Delete {bot.name} and{" "}
              {helpers.length === 1 ? "its helper" : `all ${helpers.length} helpers`}
            </strong>
            <span>
              Also deletes {names(helpers)}
              {takesFilesWithHelpers && " and the files the team shares"}.
            </span>
          </button>
          <button
            type="button"
            className="pbtn"
            style={{ alignSelf: "flex-end" }}
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="form-actions">
          <button type="button" className="pbtn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={remove.pending}
            onClick={() => void remove.run(false)}
          >
            Delete bot
          </button>
        </div>
      )}
      {remove.error && <ErrorNotice>{remove.error}</ErrorNotice>}
    </Modal>
  );
}

/** Push to this device — notifications that arrive with the app closed. */
function PushSetting() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tested, setTested] = useState(false);
  useEffect(() => {
    void pushState().then(setState, () => setState("unsupported"));
  }, []);
  if (state === null) return null;
  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      setState(state === "on" ? await disablePush() : await enablePush());
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div style={{ marginTop: 10 }}>
      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
        <input
          type="checkbox"
          checked={state === "on"}
          disabled={busy || state === "unsupported" || state === "denied"}
          onChange={() => void toggle()}
        />
        Push to this device, even when the app is closed
      </label>
      <p className="screen-help" style={{ margin: "4px 0 0 24px" }}>
        {state === "unsupported"
          ? "This browser cannot receive pushes. Install the app (from the browser's menu) or use Chrome, Edge or Firefox."
          : state === "denied"
            ? "Notifications are blocked for this site in the browser's settings."
            : "Replies, questions, approvals and sign-in requests. Install the app from the browser's menu to get its own window and icon."}
      </p>
      {state === "on" && (
        <button
          type="button"
          className="linklike"
          style={{ marginLeft: 24 }}
          onClick={() => void pushTest().then(() => setTested(true))}
        >
          {tested ? "Sent — it arrives within a few seconds" : "Send a test"}
        </button>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
    </div>
  );
}

/** `/?template=…` — a link someone shared: review it, then make your own bot from it. */
export function SharedTemplateDialog({
  token,
  onClose,
  onCreated,
}: {
  token: string;
  onClose: () => void;
  onCreated: (bot: Bot) => void;
}) {
  const fetcher = useCallback((signal: AbortSignal) => sharedTemplate(token, signal), [token]);
  const shared = useResource(fetcher);
  return (
    <Modal title="Create a bot from a shared template" onClose={onClose}>
      {shared.error && <ErrorNotice>{shared.error}</ErrorNotice>}
      {shared.loading && <p className="screen-help">Loading the template…</p>}
      {shared.data && (
        <>
          <p className="screen-help" style={{ marginTop: 0 }}>
            Someone shared this bot&apos;s setup. Read what it will do, then create your own.
            It&apos;s yours to change, and nothing you do with it goes back to them.
          </p>
          <TemplateReview
            preview={shared.data}
            source={{ token }}
            onCreated={onCreated}
            onCancel={onClose}
          />
        </>
      )}
    </Modal>
  );
}
