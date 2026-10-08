"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import { type Bot, type BotRule, deleteRule, listRules, putRule, updateBot } from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { type Appearance, Designer, appearanceFor } from "../avatar";
import { BriefSection } from "./BriefEditor";
import { MemorySection } from "./MemoryPane";

const RULE_ACTIONS = [
  ["*", "any action"],
  ["navigate", "opening a page"],
  ["click", "clicking"],
  ["type", "typing"],
  ["select", "choosing an option"],
  ["press", "pressing a key"],
] as const;

/**
 * Who this bot is and what it may do without asking.
 *
 * Profile and brief are conversation state — editing them never touches the bot's
 * actor or what it is allowed to call. The brief is its primary instruction; memory is
 * what it has learned, editable because a wrong memory is worse than none. Rules are
 * the Auto Review settings: "ask first" beats "allow automatically" when both match,
 * and no bot can change them — only the person can.
 */
export function DetailsPane({
  bot,
  bots,
  onSelect,
  onChanged,
  onDuplicate,
  onDelete,
}: {
  bot: Bot;
  bots: Bot[];
  onSelect: (id: string) => void;
  onChanged: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  return (
    <div>
      <Team bot={bot} bots={bots} onSelect={onSelect} />
      <BriefSection bot={bot} bots={bots} onChanged={onChanged} />
      <MemorySection bot={bot} />
      <Profile bot={bot} onChanged={onChanged} />
      <Looks bot={bot} onChanged={onChanged} />
      <Rules bot={bot} />
      <section className="dsec">
        <h3>About</h3>
        <dl className="kv">
          <dt>Actor</dt>
          <dd>{bot.actor_name}</dd>
          <dt>Last run</dt>
          <dd>
            {bot.last_run_id ? `${bot.run_status ?? "?"} · ${bot.last_run_id.slice(0, 8)}` : "—"}
          </dd>
          <dt>Created</dt>
          <dd>{new Date(bot.created_at).toLocaleString()}</dd>
        </dl>
        <div className="control-row">
          <button type="button" className="pbtn" onClick={onDuplicate}>
            Duplicate bot
          </button>
          <button type="button" className="pbtn danger" onClick={onDelete}>
            Delete bot
          </button>
        </div>
      </section>
    </div>
  );
}

function Team({ bot, bots, onSelect }: { bot: Bot; bots: Bot[]; onSelect: (id: string) => void }) {
  const parent = bots.find((b) => b.id === bot.parent_bot_id);
  const helpers = bots.filter((b) => b.parent_bot_id === bot.id);
  return (
    <section className="dsec">
      <h3>Team</h3>
      <dl className="kv" style={{ marginBottom: 10 }}>
        <dt>Created by</dt>
        <dd style={{ fontFamily: "var(--sans)" }}>
          {bot.created_by === "bot" && parent ? (
            <button type="button" className="linklike" onClick={() => onSelect(parent.id)}>
              {parent.name}
            </button>
          ) : bot.created_by === "bot" ? (
            "a bot that has since been deleted"
          ) : (
            "you"
          )}
        </dd>
        {parent && bot.created_by !== "bot" && (
          <>
            <dt>Reports to</dt>
            <dd>{parent.name}</dd>
          </>
        )}
      </dl>
      {helpers.length > 0 ? (
        <div className="helper-list">
          {helpers.map((h) => (
            <button key={h.id} type="button" className="chipbtn" onClick={() => onSelect(h.id)}>
              {h.avatar || "🤖"} {h.name}
              {h.working ? " · working" : ""}
            </button>
          ))}
        </div>
      ) : (
        <p className="screen-help" style={{ margin: 0 }}>
          No helpers yet. {bot.name} creates a helper bot when a task needs one, and asks it for
          work — you&apos;ll see each handoff in this conversation.
        </p>
      )}
    </section>
  );
}

function Looks({ bot, onChanged }: { bot: Bot; onChanged: () => void }) {
  const saved = appearanceFor(bot);
  const [look, setLook] = useState<Appearance>(saved);
  useEffect(() => setLook(appearanceFor(bot)), [bot.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = useAction(() => updateBot(bot.id, { appearance: look }), {
    onDone: onChanged,
  });
  const dirty = JSON.stringify(look) !== JSON.stringify(saved);
  return (
    <section className="dsec">
      <h3>Appearance</h3>
      <Designer value={look} onChange={setLook} previewSize={240} />
      {save.error && <ErrorNotice>{save.error}</ErrorNotice>}
      <div className="form-actions" style={{ marginTop: 10 }}>
        <button type="button" className="pbtn" disabled={!dirty} onClick={() => setLook(saved)}>
          Reset
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={!dirty || save.pending}
          onClick={() => void save.run()}
        >
          Save look
        </button>
      </div>
    </section>
  );
}

function Profile({ bot, onChanged }: { bot: Bot; onChanged: () => void }) {
  const [form, setForm] = useState(() => pick(bot));
  useEffect(() => setForm(pick(bot)), [bot.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = useAction(() => updateBot(bot.id, form), { onDone: onChanged });
  const dirty = JSON.stringify(form) !== JSON.stringify(pick(bot));

  return (
    <section className="dsec">
      <h3>Profile</h3>
      <div className="form">
        <div className="form-row">
          <label>
            Name
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
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
          Description <small>— one line, shown in the sidebar and search</small>
          <textarea
            rows={2}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        {save.error && <ErrorNotice>{save.error}</ErrorNotice>}
        <div className="form-actions">
          {save.result && !dirty && <span className="note">Saved</span>}
          <button
            type="button"
            className="pbtn"
            disabled={!dirty || save.pending}
            onClick={() => setForm(pick(bot))}
          >
            Reset
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={!dirty || save.pending || !form.name.trim()}
            onClick={() => void save.run()}
          >
            Save
          </button>
        </div>
      </div>
    </section>
  );
}

function pick(bot: Bot) {
  return {
    name: bot.name,
    label: bot.label,
    description: bot.description,
    avatar: bot.avatar,
  };
}

function Rules({ bot }: { bot: Bot }) {
  const fetcher = useCallback((signal: AbortSignal) => listRules(bot.id, signal), [bot.id]);
  const rules = useResource(fetcher, { intervalMs: 5000 });
  const [draft, setDraft] = useState<Pick<BotRule, "action_type" | "host" | "decision">>({
    action_type: "*",
    host: "",
    decision: "ask",
  });
  const add = useAction(() => putRule(bot.id, draft), {
    onDone: () => {
      setDraft({ ...draft, host: "" });
      rules.refresh();
    },
  });
  const remove = useAction((id: string) => deleteRule(bot.id, id), {
    onDone: rules.refresh,
  });

  return (
    <section className="dsec">
      <h3>Approvals</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        By default {bot.name} asks before typing into password fields and before anything it judges
        consequential (orders, sending, posting, deleting). Add rules to change that. When rules
        conflict, <strong>ask first</strong> wins.
      </p>
      {rules.data?.rules.map((r) => (
        <div key={r.id} className="rule">
          <span className={`decision ${r.decision}`}>
            {r.decision === "ask" ? "Ask first" : "Allow"}
          </span>
          <span className="grow">
            {RULE_ACTIONS.find(([k]) => k === r.action_type)?.[1] ?? r.action_type}
            {r.host ? (
              <>
                {" "}
                on <code>{r.host}</code>
              </>
            ) : (
              " everywhere"
            )}
          </span>
          <button
            type="button"
            className="ibtn"
            aria-label="Remove rule"
            disabled={remove.pending}
            onClick={() => void remove.run(r.id)}
          >
            ✕
          </button>
        </div>
      ))}
      {rules.data?.rules.length === 0 && <p className="screen-help">No rules yet.</p>}
      <div className="form" style={{ marginTop: 10 }}>
        <div className="form-row">
          <label>
            When
            <select
              value={draft.decision}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  decision: e.target.value as "ask" | "allow",
                })
              }
            >
              <option value="ask">Ask first</option>
              <option value="allow">Allow automatically</option>
            </select>
          </label>
          <label>
            Action
            <select
              value={draft.action_type}
              onChange={(e) => setDraft({ ...draft, action_type: e.target.value })}
            >
              {RULE_ACTIONS.map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="form-row">
          <label>
            On site <small>(blank for every site)</small>
            <input
              value={draft.host}
              placeholder="example.com"
              onChange={(e) => setDraft({ ...draft, host: e.target.value })}
            />
          </label>
          <button
            type="button"
            className="pbtn"
            style={{ flex: "none" }}
            disabled={add.pending}
            onClick={() => void add.run()}
          >
            Add rule
          </button>
        </div>
        {(add.error || remove.error) && <ErrorNotice>{add.error || remove.error}</ErrorNotice>}
      </div>
    </section>
  );
}
