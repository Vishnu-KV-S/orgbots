"use client";

import { useCallback, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type BotTemplate,
  type TemplateLink,
  type TemplatePreview,
  exportTemplate,
  importTemplate,
  listTemplateLinks,
  makeTemplateLink,
  previewTemplate,
  revokeTemplateLink,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { DECISION_LABEL, ruleAction } from "../lib/rules";
import { Avatar } from "./Avatar";

/**
 * Bot templates: a bot's setup as a file or a link, and the review before a bot is made
 * from one. What travels is the setup — profile, look, brief, approval rules, routines,
 * Auto Review — never what the bot learned or was given. The review shows all of it;
 * the template's "allow" rules are only kept if the person ticks the box, and its
 * routines arrive paused (`domain/templates.py` enforces both on the server).
 */

const MAX_FILE_BYTES = 512 * 1024;

export function templateLink(token: string): string {
  return `${window.location.origin}/?template=${encodeURIComponent(token)}`;
}

/** A template file the person picked, checked by the server. */
export async function readTemplateFile(file: File): Promise<TemplatePreview> {
  if (file.size > MAX_FILE_BYTES) throw new Error("That file is too big to be a bot template.");
  let parsed: unknown;
  try {
    parsed = JSON.parse(await file.text());
  } catch {
    throw new Error("That file isn't a bot template — it isn't JSON.");
  }
  return previewTemplate(parsed);
}

function download(name: string, template: BotTemplate) {
  const blob = new Blob([JSON.stringify(template, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export function ShareSection({ bot }: { bot: Bot }) {
  const fetcher = useCallback((signal: AbortSignal) => listTemplateLinks(bot.id, signal), [bot.id]);
  const links = useResource(fetcher);
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (link: TemplateLink) => {
    try {
      await navigator.clipboard.writeText(templateLink(link.token));
      setCopied(link.id);
    } catch {
      /* the link is in the field to copy by hand */
    }
  };
  const save = useAction(() => exportTemplate(bot.id), {
    onDone: ({ template, file_name }) => download(file_name, template),
  });
  const make = useAction(() => makeTemplateLink(bot.id), {
    onDone: (link) => {
      links.refresh();
      void copy(link);
    },
  });
  const revoke = useAction((id: string) => revokeTemplateLink(bot.id, id), {
    onDone: links.refresh,
  });
  const list = links.data?.links ?? [];
  const error = save.error || make.error || revoke.error || links.error;

  return (
    <section className="dsec">
      <h3>Share as a template</h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        Let someone start their own {bot.name} with its profile, look, brief, approval rules and
        routines. Its memories, conversations, saved sign-ins, files and connected apps are never
        shared. Whoever imports it decides whether to keep its “allow” rules, and its routines
        arrive paused.
      </p>
      <div className="control-row">
        <button
          type="button"
          className="pbtn"
          disabled={save.pending}
          onClick={() => void save.run()}
        >
          Save as a file
        </button>
        <button
          type="button"
          className="pbtn"
          disabled={make.pending}
          onClick={() => void make.run()}
        >
          {make.pending ? "Making…" : "Make a link"}
        </button>
      </div>
      {list.map((link) => (
        <div key={link.id} className="share-link">
          <input
            className="input"
            readOnly
            aria-label="Template link"
            value={templateLink(link.token)}
            onFocus={(e) => e.currentTarget.select()}
          />
          <button type="button" className="pbtn" onClick={() => void copy(link)}>
            {copied === link.id ? "Copied" : "Copy"}
          </button>
          <button
            type="button"
            className="pbtn danger"
            disabled={revoke.pending}
            onClick={() => void revoke.run(link.id)}
          >
            Turn off
          </button>
          <div className="routine-meta">
            Made {new Date(link.created_at).toLocaleString()} · used{" "}
            {link.uses === 1 ? "once" : `${link.uses} times`}
          </div>
        </div>
      ))}
      {list.length > 0 && (
        <p className="screen-help">
          A link shares {bot.name} as it was when you made it, so later changes aren&apos;t in it.
          Anyone with the link can read the brief and rules.
        </p>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
    </section>
  );
}

/** What a template will make, and the button that makes it. */
export function TemplateReview({
  preview,
  source,
  onCreated,
  onCancel,
  cancelLabel = "Cancel",
}: {
  preview: TemplatePreview;
  source: { template: BotTemplate } | { token: string };
  onCreated: (bot: Bot) => void;
  onCancel: () => void;
  cancelLabel?: string;
}) {
  const t = preview.template;
  const { plan } = preview;
  const [name, setName] = useState(t.name);
  const [keepAllows, setKeepAllows] = useState(false);
  const create = useAction(
    () => importTemplate(source, { name: name.trim(), keep_allows: keepAllows }),
    { onDone: onCreated },
  );
  const brief = t.brief;

  return (
    <div className="form template-review">
      <div className="template-head">
        <Avatar appearance={t.appearance ?? undefined} bot={{ id: t.name }} size={56} />
        <label className="grow">
          Name
          <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        </label>
      </div>
      {(t.label || t.description) && (
        <p className="template-desc">
          {t.label && <strong>{t.label}. </strong>}
          {t.description}
        </p>
      )}

      <div className="template-part">
        <h4>Brief</h4>
        {brief.mission ? <p>{brief.mission}</p> : <p className="muted">No mission written.</p>}
        {brief.duties.length > 0 && (
          <ul>
            {brief.duties.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        )}
        {brief.boundaries.length > 0 && (
          <>
            <div className="muted">Never:</div>
            <ul>
              {brief.boundaries.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </>
        )}
        {brief.style && <p className="muted">Style: {brief.style}</p>}
        {brief.escalation && <p className="muted">Asks: {brief.escalation}</p>}
        {brief.notes && <p className="muted template-notes">{brief.notes}</p>}
      </div>

      <div className="template-part">
        <h4>Approvals</h4>
        {plan.rules.length === 0 && plan.allows.length === 0 && (
          <p className="muted">No rules: it asks before anything consequential.</p>
        )}
        {plan.rules.map((r) => (
          <div key={`${r.action_type}:${r.host}`} className="rule">
            <span className={`decision ${r.decision}`}>{DECISION_LABEL[r.decision]}</span>
            <span className="grow">
              {ruleAction(r.action_type)}
              {r.host ? (
                <>
                  {" "}
                  on <code>{r.host}</code>
                </>
              ) : (
                " everywhere"
              )}
            </span>
          </div>
        ))}
        {plan.allows.length > 0 && (
          <label className="check-row template-allows">
            <input
              type="checkbox"
              checked={keepAllows}
              onChange={(e) => setKeepAllows(e.target.checked)}
            />
            <span>
              Also let it go ahead without asking for{" "}
              {plan.allows
                .map((r) => ruleAction(r.action_type) + (r.host ? ` on ${r.host}` : " everywhere"))
                .join("; ")}
              . Leave this off unless you trust whoever made the template.
            </span>
          </label>
        )}
        {t.auto_review && <p className="muted">Auto Review is on.</p>}
      </div>

      <div className="template-part">
        <h4>Routines</h4>
        {plan.routines.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <>
            {plan.routines.map((r) => (
              <div key={r.name} className="template-routine">
                <strong>{r.name}</strong> <span className="muted">· {r.when} · paused</span>
                <div className="muted">{r.instruction}</div>
              </div>
            ))}
            <p className="screen-help" style={{ margin: "4px 0 0" }}>
              They arrive paused. Turn each one on in Details once you&apos;ve read it.
            </p>
          </>
        )}
      </div>

      {create.error && <ErrorNotice>{create.error}</ErrorNotice>}
      <div className="form-actions">
        <button type="button" className="pbtn" onClick={onCancel}>
          {cancelLabel}
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={!name.trim() || create.pending}
          onClick={() => void create.run()}
        >
          {create.pending ? "Creating…" : "Create bot"}
        </button>
      </div>
    </div>
  );
}
