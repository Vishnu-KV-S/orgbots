"use client";

import { useCallback, useMemo, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotMemory,
  type MemoryKind,
  addMemory,
  clearMemories,
  deleteMemory,
  editMemory,
  listMemories,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { timeAgo } from "../lib/text";

const KINDS: { kind: MemoryKind; label: string; hint: string }[] = [
  { kind: "preference", label: "Preferences", hint: "How you like things done" },
  { kind: "person", label: "People", hint: "Contacts and how to reach them" },
  { kind: "fact", label: "Facts", hint: "About you and your work" },
  { kind: "skill", label: "Skills", hint: "Steps that worked before" },
  { kind: "episode", label: "Diary", hint: "A line for every finished task" },
];

const KIND_LABEL: Record<MemoryKind, string> = {
  preference: "Preference",
  person: "Person",
  fact: "Fact",
  skill: "Skill",
  episode: "Diary",
};

function source(m: BotMemory, bot: Bot): string {
  if (m.source_kind === "person") return "added by you";
  if (m.source_kind === "parent") return `taught by ${m.source_name || "its parent"}`;
  if (m.kind === "episode") return "diary";
  return `learned by ${bot.name}`;
}

function Importance({
  value,
  onChange,
  disabled,
}: {
  value: number;
  onChange?: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <span className="importance" aria-label={`Importance ${value} of 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <button
          key={i}
          type="button"
          className={cx("pip", i <= value && "on")}
          disabled={disabled || !onChange}
          aria-label={`Importance ${i}`}
          onClick={() => onChange?.(i)}
        />
      ))}
    </span>
  );
}

function Row({ bot, m, onChanged }: { bot: Bot; m: BotMemory; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.content);
  const edit = useAction(
    (fields: Partial<Pick<BotMemory, "content" | "importance" | "pinned">>) =>
      editMemory(bot.id, m.id, fields),
    { onDone: onChanged },
  );
  const remove = useAction(() => deleteMemory(bot.id, m.id), { onDone: onChanged });
  return (
    <li className={cx("mem", m.pinned && "pinned")}>
      {editing ? (
        <div className="form">
          <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} autoFocus />
          <div className="form-actions">
            <button
              type="button"
              className="pbtn"
              onClick={() => {
                setText(m.content);
                setEditing(false);
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="pbtn primary"
              disabled={!text.trim() || edit.pending}
              onClick={() =>
                void edit.run({ content: text.trim() }).then((r) => r !== null && setEditing(false))
              }
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <p className="mem-text">{m.content}</p>
      )}
      <div className="mem-meta">
        <span className="kind-chip">{KIND_LABEL[m.kind]}</span>
        <Importance
          value={m.importance}
          disabled={edit.pending}
          onChange={(importance) => void edit.run({ importance })}
        />
        <span className="muted">
          {source(m, bot)} · {timeAgo(m.created_at)}
          {m.recall_count > 0 ? ` · recalled ${m.recall_count}×` : ""}
        </span>
        <span className="mem-tools">
          <button
            type="button"
            className="linklike"
            aria-pressed={m.pinned}
            title="Pinned memories are in every prompt and are never forgotten"
            onClick={() => void edit.run({ pinned: !m.pinned })}
          >
            {m.pinned ? "Unpin" : "Pin"}
          </button>
          {!editing && (
            <button type="button" className="linklike" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          <button
            type="button"
            className="linklike"
            disabled={remove.pending}
            onClick={() => void remove.run()}
          >
            Forget
          </button>
        </span>
      </div>
      {(edit.error || remove.error) && <ErrorNotice>{edit.error || remove.error}</ErrorNotice>}
    </li>
  );
}

/**
 * Everything the bot remembers, the way it is organised in its head: by kind, with how
 * important each memory is, where it came from, and how often it has come to mind.
 * Each prompt carries only what is relevant — pinned memories and strong preferences
 * always — and the bot can search the rest. A wrong memory is worse than none, so
 * every one can be corrected or forgotten here.
 */
export function MemorySection({ bot }: { bot: Bot }) {
  const fetcher = useCallback((signal: AbortSignal) => listMemories(bot.id, signal), [bot.id]);
  const memories = useResource(fetcher, { intervalMs: 6000 });
  const [filter, setFilter] = useState<MemoryKind | "all">("all");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState({
    content: "",
    kind: "preference" as MemoryKind,
    importance: 3,
  });

  const all = useMemo(() => memories.data?.memories ?? [], [memories.data]);
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const m of all) out[m.kind] = (out[m.kind] ?? 0) + 1;
    return out;
  }, [all]);
  const shown = all.filter(
    (m) =>
      (filter === "all" || m.kind === filter) &&
      (!query.trim() || m.content.toLowerCase().includes(query.trim().toLowerCase())),
  );

  const add = useAction(() => addMemory(bot.id, { ...draft, pinned: false }), {
    onDone: () => {
      setDraft({ ...draft, content: "" });
      memories.refresh();
    },
  });
  const clear = useAction(() => clearMemories(bot.id), { onDone: memories.refresh });

  return (
    <section className="dsec">
      <h3>
        Memory <span className="count">{all.length}</span>
      </h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        What {bot.name} has learned, kept like a person&apos;s memory. Only what&apos;s relevant
        comes to mind on each task, and {bot.name} can search the rest. Pinned memories are always
        in mind and never forgotten.
      </p>

      <div className="seg wrap" role="tablist" aria-label="Kind">
        <button
          type="button"
          role="tab"
          aria-selected={filter === "all"}
          className={cx(filter === "all" && "on")}
          onClick={() => setFilter("all")}
        >
          All
        </button>
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="tab"
            title={k.hint}
            aria-selected={filter === k.kind}
            className={cx(filter === k.kind && "on")}
            onClick={() => setFilter(k.kind)}
          >
            {k.label}
            {counts[k.kind] ? ` ${counts[k.kind]}` : ""}
          </button>
        ))}
      </div>

      {all.length > 6 && (
        <input
          className="mem-search"
          value={query}
          placeholder="Search memories…"
          onChange={(e) => setQuery(e.target.value)}
        />
      )}

      {memories.error && <ErrorNotice>{memories.error}</ErrorNotice>}
      {shown.length > 0 ? (
        <ul className="mems">
          {shown.map((m) => (
            <Row key={`${m.id}:${m.updated_at}`} bot={bot} m={m} onChanged={memories.refresh} />
          ))}
        </ul>
      ) : (
        <p className="screen-help">
          {all.length === 0
            ? `Nothing remembered yet. ${bot.name} saves what matters as it works, and writes a diary line after every task.`
            : "No memories match."}
        </p>
      )}

      <div className="form" style={{ marginTop: 10 }}>
        <label>
          Teach {bot.name} something
          <textarea
            rows={2}
            value={draft.content}
            placeholder="e.g. I prefer aisle seats and never fly before 9am"
            onChange={(e) => setDraft({ ...draft, content: e.target.value })}
          />
        </label>
        <div className="form-row">
          <label>
            Kind
            <select
              value={draft.kind}
              onChange={(e) => setDraft({ ...draft, kind: e.target.value as MemoryKind })}
            >
              {KINDS.map((k) => (
                <option key={k.kind} value={k.kind}>
                  {k.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Importance
            <Importance
              value={draft.importance}
              onChange={(importance) => setDraft({ ...draft, importance })}
            />
          </label>
        </div>
        {(add.error || clear.error) && <ErrorNotice>{add.error || clear.error}</ErrorNotice>}
        <div className="form-actions">
          <button
            type="button"
            className="pbtn"
            disabled={all.length === 0 || clear.pending}
            onClick={() => {
              if (window.confirm(`Clear everything ${bot.name} remembers? This cannot be undone.`))
                void clear.run();
            }}
          >
            Clear all
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={!draft.content.trim() || add.pending}
            onClick={() => void add.run()}
          >
            Remember
          </button>
        </div>
      </div>
    </section>
  );
}
