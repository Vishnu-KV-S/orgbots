"use client";

import { ArrowLeftIcon, PlusIcon } from "@heroicons/react/24/outline";
import { useEffect, useMemo, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  EMPTY_SKILL,
  type MarketSkill,
  type Skill,
  type SkillFields,
  createSkill,
  deleteSkill,
  installSkill,
  listMarketplace,
  listSkills,
  updateSkill,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { timeAgo } from "../lib/text";

const SOURCE: Record<Skill["source"], string> = {
  person: "written by you",
  bot: "saved by a bot",
  demonstration: "from a demonstration",
  marketplace: "from the marketplace",
};

type View = { kind: "library" } | { kind: "market" } | { kind: "edit"; skill: Skill | null };

/**
 * The organization's skills — how-tos every bot can follow.
 *
 * A bot loads one when a task matches it, or when you name it as `/name` in a message.
 * A skill written up from a demonstration is a draft: read it, fix what was an accident
 * of how you clicked, and mark it ready — until then no bot is offered it.
 */
export function SkillsPane({
  openId,
  onOpened,
}: {
  openId?: string | null;
  onOpened?: () => void;
}) {
  const skills = useResource(listSkills, { intervalMs: 6000 });
  const [view, setView] = useState<View>({ kind: "library" });
  const [query, setQuery] = useState("");
  const list = skills.data?.skills ?? [];

  useEffect(() => {
    if (!openId) return;
    const found = list.find((s) => s.id === openId);
    if (found) {
      setView({ kind: "edit", skill: found });
      onOpened?.();
    }
  }, [openId, list, onOpened]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter((s) =>
      `${s.name} ${s.title} ${s.when} ${s.steps.join(" ")}`.toLowerCase().includes(q),
    );
  }, [list, query]);
  const drafts = list.filter((s) => s.status === "draft").length;

  if (view.kind === "edit") {
    return (
      <SkillEditor
        skill={view.skill}
        onClose={() => setView({ kind: "library" })}
        onSaved={() => {
          skills.refresh();
          setView({ kind: "library" });
        }}
      />
    );
  }

  return (
    <div className="skills">
      <div className="bpane-tabs" style={{ padding: 0, border: "none", marginBottom: 10 }}>
        <button
          type="button"
          className={cx("tab", view.kind === "library" && "on")}
          onClick={() => setView({ kind: "library" })}
        >
          Library{list.length ? ` · ${list.length}` : ""}
        </button>
        <button
          type="button"
          className={cx("tab", view.kind === "market" && "on")}
          onClick={() => setView({ kind: "market" })}
        >
          Marketplace
        </button>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          className="pbtn"
          onClick={() => setView({ kind: "edit", skill: null })}
        >
          <PlusIcon /> New skill
        </button>
      </div>

      {view.kind === "market" ? (
        <Marketplace onInstalled={skills.refresh} />
      ) : (
        <>
          <p className="screen-help" style={{ marginTop: 0 }}>
            How-tos every bot can follow. Type <code>/name</code> in a message to hand one to a bot,
            or let it pick the right one. Teach a new one by doing the task yourself in the Computer
            pane, or ask a bot to save how it just did something.
          </p>
          {drafts > 0 && (
            <p className="skill-note">
              {drafts} draft{drafts === 1 ? "" : "s"} to review — bots don&apos;t use a draft until
              you mark it ready.
            </p>
          )}
          <input
            className="input"
            style={{ width: "100%", marginBottom: 8 }}
            value={query}
            placeholder="Search skills"
            onChange={(e) => setQuery(e.target.value)}
          />
          {shown.map((s) => (
            <button
              key={s.id}
              type="button"
              className="skill-row"
              onClick={() => setView({ kind: "edit", skill: s })}
            >
              <span className="skill-name">/{s.name}</span>
              {s.status === "draft" && <span className="skill-badge">Draft</span>}
              <span className="skill-title">{s.title || s.when || `${s.steps.length} steps`}</span>
              <span className="muted">
                {SOURCE[s.source]}
                {s.use_count > 0 ? ` · used ${s.use_count}×` : ""} · {timeAgo(s.updated_at)}
              </span>
            </button>
          ))}
          {skills.data && list.length === 0 && (
            <p className="screen-help">
              No skills yet. Install a few from the Marketplace, or write your own.
            </p>
          )}
          {skills.error && <ErrorNotice>{skills.error}</ErrorNotice>}
        </>
      )}
    </div>
  );
}

function Marketplace({ onInstalled }: { onInstalled: () => void }) {
  const market = useResource(listMarketplace, { intervalMs: 0 });
  const [open, setOpen] = useState<string | null>(null);
  const install = useAction((key: string) => installSkill(key), {
    onDone: () => {
      market.refresh();
      onInstalled();
    },
  });
  return (
    <div>
      <p className="screen-help" style={{ marginTop: 0 }}>
        Packaged skills that ship with the runtime. Installing copies one into your library, where
        you can read and change it like your own.
      </p>
      {market.data?.skills.map((m: MarketSkill) => (
        <div key={m.key} className="market-item">
          <div className="market-head">
            <button
              type="button"
              className="linklike"
              onClick={() => setOpen(open === m.key ? null : m.key)}
            >
              /{m.name}
            </button>
            <span className="muted">{m.category}</span>
            <span style={{ flex: 1 }} />
            {m.installed ? (
              <span className="skill-badge ok">Installed</span>
            ) : (
              <button
                type="button"
                className="pbtn"
                disabled={install.pending}
                onClick={() => void install.run(m.key)}
              >
                Install
              </button>
            )}
          </div>
          <div className="market-blurb">{m.blurb}</div>
          {open === m.key && (
            <ol className="market-steps">
              {m.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          )}
        </div>
      ))}
      {install.error && <ErrorNotice>{install.error}</ErrorNotice>}
      {market.error && <ErrorNotice>{market.error}</ErrorNotice>}
    </div>
  );
}

function fieldsOf(skill: Skill | null): SkillFields & { name: string } {
  if (!skill) return { ...EMPTY_SKILL, name: "" };
  return {
    name: skill.name,
    title: skill.title,
    when: skill.when,
    inputs: skill.inputs,
    steps: skill.steps,
    checks: skill.checks,
    output: skill.output,
    approvals: skill.approvals,
  };
}

function SkillEditor({
  skill,
  onClose,
  onSaved,
}: {
  skill: Skill | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState(() => fieldsOf(skill));
  const [stepsText, setStepsText] = useState(() => (skill?.steps ?? []).join("\n"));
  const [ready, setReady] = useState(skill ? skill.status === "ready" : true);
  const set = (fields: Partial<typeof form>) => setForm((f) => ({ ...f, ...fields }));
  const steps = stepsText
    .split("\n")
    .map((s) => s.replace(/^\s*(\d+[.)]|[-*•])\s*/, "").trim())
    .filter(Boolean);

  const save = useAction(
    () => {
      const body = { ...form, steps, status: ready ? ("ready" as const) : ("draft" as const) };
      return skill ? updateSkill(skill.id, body) : createSkill(body);
    },
    { onDone: onSaved },
  );
  const remove = useAction(() => deleteSkill(skill?.id ?? ""), { onDone: onSaved });

  return (
    <div className="form skill-editor">
      <div className="skill-editor-head">
        <button type="button" className="ibtn" onClick={onClose} aria-label="Back to skills">
          <ArrowLeftIcon />
        </button>
        <strong>{skill ? `/${skill.name}` : "New skill"}</strong>
        {skill && (
          <span className="muted">
            v{skill.version} · {SOURCE[skill.source]}
          </span>
        )}
      </div>
      {skill?.status === "draft" && skill.source === "demonstration" && (
        <p className="skill-note">
          Written up by {skill.updated_by_name || "a bot"} from your demonstration. Read the steps,
          make them general (what to look for, not where you happened to click), then mark it ready.
        </p>
      )}
      <div className="form-row">
        <label>
          <span>
            Name <small>— called as /name</small>
          </span>
          <input
            value={form.name}
            placeholder="weekly-vendor-check"
            onChange={(e) => set({ name: e.target.value })}
          />
        </label>
        <label>
          Title
          <input
            value={form.title}
            placeholder="Weekly vendor check"
            onChange={(e) => set({ title: e.target.value })}
          />
        </label>
      </div>
      <label>
        When to use it
        <textarea rows={2} value={form.when} onChange={(e) => set({ when: e.target.value })} />
      </label>
      <label>
        <span>
          What it needs <small>— information, files, sites, sign-ins</small>
        </span>
        <textarea rows={2} value={form.inputs} onChange={(e) => set({ inputs: e.target.value })} />
      </label>
      <label>
        <span>
          Steps <small>— one per line</small>
        </span>
        <textarea rows={7} value={stepsText} onChange={(e) => setStepsText(e.target.value)} />
      </label>
      <label>
        How to check the result
        <textarea rows={2} value={form.checks} onChange={(e) => set({ checks: e.target.value })} />
      </label>
      <label>
        What to hand back
        <textarea rows={2} value={form.output} onChange={(e) => set({ output: e.target.value })} />
      </label>
      <label>
        Needs your approval
        <textarea
          rows={2}
          value={form.approvals}
          placeholder="Sending, paying, posting, deleting"
          onChange={(e) => set({ approvals: e.target.value })}
        />
      </label>
      <label className="check-row">
        <input type="checkbox" checked={ready} onChange={(e) => setReady(e.target.checked)} />
        Ready — bots may use it
      </label>
      {(save.error || remove.error) && <ErrorNotice>{save.error || remove.error}</ErrorNotice>}
      <div className="form-actions">
        {skill && (
          <button
            type="button"
            className="pbtn danger"
            style={{ marginRight: "auto" }}
            disabled={remove.pending}
            onClick={() => {
              if (window.confirm(`Delete the skill /${skill.name}?`)) void remove.run();
            }}
          >
            Delete
          </button>
        )}
        <button type="button" className="pbtn" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={save.pending || steps.length === 0 || !(form.name || form.title).trim()}
          onClick={() => void save.run()}
        >
          {skill ? "Save skill" : "Create skill"}
        </button>
      </div>
    </div>
  );
}
