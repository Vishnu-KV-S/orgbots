"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotDraft,
  type ComputerStatus,
  type SearchResult,
  createBot,
  resetComputer,
  searchBots,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { cx } from "@/lib/cx";
import { Designer, randomAppearance } from "../avatar";
import { TEMPLATES } from "../lib/templates";
import { Avatar } from "./Avatar";
import { BriefFields } from "./BriefEditor";

function Modal({
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
            ✕
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
  const create = useAction((draft: BotDraft) => createBot(draft), {
    onDone: (bot) => onCreated(bot),
  });

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
          All your bots share one cloud computer — the same browser profile, sign-ins and cookies —
          and each has its own screen. Recover restarts the browser; sign-ins survive.
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
        {bot.name}&apos;s conversation and memory will be deleted. This cannot be undone.
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
              conversations.
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
            <span>Also deletes {names(helpers)}.</span>
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
