"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotRule,
  deleteRule,
  deleteVaultEntry,
  listRules,
  listVault,
  putRule,
  setVaultAutoUse,
  updateBot,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { type Appearance, Designer, appearanceFor } from "../avatar";
import { BriefSection } from "./BriefEditor";
import { MemorySection } from "./MemoryPane";
import { RoutinesSection } from "./RoutinesPane";

const RULE_ACTIONS = [
  ["*", "any action"],
  ["navigate", "opening a page"],
  ["click", "clicking"],
  ["type", "typing"],
  ["select", "choosing an option"],
  ["press", "pressing a key"],
  ["sign_in", "signing in with a saved login"],
  ["run_command", "running commands in the sandbox"],
  ["run_local", "running commands on this computer"],
  ["use_connector", "using a connected app (name it under On site)"],
] as const;

const DECISION_LABEL = { ask: "Ask first", allow: "Allow", deny: "Never" } as const;

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
      <RoutinesSection bot={bot} />
      <MemorySection bot={bot} />
      <Profile bot={bot} onChanged={onChanged} />
      <Looks bot={bot} onChanged={onChanged} />
      <Rules bot={bot} onChanged={onChanged} />
      <SavedLogins bot={bot} />
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

function Rules({ bot, onChanged }: { bot: Bot; onChanged: () => void }) {
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
  const review = useAction((on: boolean) => updateBot(bot.id, { auto_review: on }), {
    onDone: onChanged,
  });

  return (
    <section className="dsec">
      <h3>Approvals</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        By default {bot.name} asks before anything it judges consequential (orders, sending,
        posting, deleting) and before every command on this computer. Passwords and codes never
        go through it: they go through the secure sign-in form. Add rules to change that.{" "}
        <strong>Never</strong> beats <strong>ask first</strong>, which beats allow.
      </p>
      <label className="check-row review-toggle">
        <input
          type="checkbox"
          checked={bot.auto_review}
          disabled={review.pending}
          onChange={(e) => void review.run(e.target.checked)}
        />
        <span>
          <strong>Auto Review</strong> — a second model checks {bot.name}&apos;s risky steps
          (sending, buying, deleting, commands, handing work to other bots) against what you
          asked. A concern asks you; a clear mismatch is refused. An “allow” rule then only lets
          a step through when the reviewer has no concerns.
        </span>
      </label>
      {review.error && <ErrorNotice>{review.error}</ErrorNotice>}
      {rules.data?.rules.map((r) => (
        <div key={r.id} className="rule">
          <span className={`decision ${r.decision}`}>{DECISION_LABEL[r.decision]}</span>
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
                  decision: e.target.value as BotRule["decision"],
                })
              }
            >
              <option value="ask">Ask first</option>
              <option value="allow">Allow automatically</option>
              <option value="deny">Never allow</option>
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

/**
 * The login vault, as far as anyone can see it: sites and hints. Every bot in this
 * organization shares it, the way they share the browser's cookies. No value is ever
 * sent to this page; a login is changed by entering it again on a sign-in card.
 */
function SavedLogins({ bot }: { bot: Bot }) {
  const vault = useResource(listVault, { intervalMs: 15000 });
  const toggle = useAction(
    (id: string, autoUse: boolean) => setVaultAutoUse(id, autoUse),
    { onDone: vault.refresh },
  );
  const remove = useAction((id: string) => deleteVaultEntry(id), { onDone: vault.refresh });
  const entries = vault.data?.entries ?? [];

  return (
    <section className="dsec">
      <h3>Saved logins</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        When {bot.name} reaches a sign-in page, it asks you in the chat. What you enter is
        encrypted and typed straight into the browser. No bot ever sees it. Logins you save
        here are shared by all your bots and filled in automatically unless you switch that off.
        Codes are never saved.
      </p>
      {entries.map((e) => (
        <div key={e.id} className="rule">
          <span className="grow">
            <code>{e.host}</code> {e.label}
            <span className="muted">
              {" "}
              · {e.use_count > 0 ? `used ${e.use_count}×` : "not used yet"}
            </span>
          </span>
          <label className="creds-auto" title="Fill this login in without asking">
            <input
              type="checkbox"
              checked={e.auto_use}
              disabled={toggle.pending}
              onChange={(ev) => void toggle.run(e.id, ev.target.checked)}
            />
            Auto
          </label>
          <button
            type="button"
            className="ibtn"
            aria-label={`Delete saved login for ${e.host}`}
            disabled={remove.pending}
            onClick={() => void remove.run(e.id)}
          >
            ✕
          </button>
        </div>
      ))}
      {vault.data && entries.length === 0 && <p className="screen-help">No saved logins yet.</p>}
      {vault.error && <ErrorNotice>{vault.error}</ErrorNotice>}
      {(toggle.error || remove.error) && <ErrorNotice>{toggle.error || remove.error}</ErrorNotice>}
    </section>
  );
}
