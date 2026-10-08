"use client";

import { decide, type BotMessage } from "@/lib/api/bots";
import { ErrorNotice } from "@/components/ui";
import { useAction } from "@/lib/hooks/useAction";
import { cx } from "@/lib/cx";
import { describeAction } from "../lib/text";

const DECIDED: Record<string, string> = {
  once: "Allowed once",
  always: "Always allowed",
  deny: "Denied",
};

/**
 * The bot wants to do something consequential and is waiting.
 *
 * The card shows the operation and its real inputs — the URL, the element, the
 * text that will be typed (dots for a secret) — built by the runtime from the
 * action itself, not from the bot's description of it. The bot's reasoning is
 * shown underneath as what it is: the bot's account of why.
 */
export function ApprovalCard({
  botId,
  message,
  live,
  decision,
  onDecided,
}: {
  botId: string;
  message: BotMessage;
  live: boolean;
  decision: string | null;
  onDecided: () => void;
}) {
  const action = message.payload.action;
  const pendingId = message.payload.pending_id ?? "";
  const act = useAction((d: "once" | "always" | "deny") => decide(botId, pendingId, d), {
    onDone: onDecided,
  });
  const host = action?.host || (action?.page_url ? safeHost(action.page_url) : "");

  return (
    <div className={cx("approval", !live && "decided")}>
      <div className="approval-title">
        {live ? <span className="badge-wait">Needs approval</span> : null}
        <span>{describeAction(action)}</span>
      </div>
      <dl>
        <dt>Action</dt>
        <dd>{action?.type}</dd>
        {action?.url && (
          <>
            <dt>URL</dt>
            <dd>{action.url}</dd>
          </>
        )}
        {action?.element_label && (
          <>
            <dt>Element</dt>
            <dd>
              [{action.element}] {action.element_label}
            </dd>
          </>
        )}
        {action?.text !== undefined && (
          <>
            <dt>Text</dt>
            <dd>{action.text}</dd>
          </>
        )}
        {action?.page_url && (
          <>
            <dt>On page</dt>
            <dd>{action.page_url}</dd>
          </>
        )}
      </dl>
      {message.payload.reason && <div className="reason">Why asking: {message.payload.reason}</div>}
      {message.content && <div className="reason">Bot: {message.content}</div>}
      {act.error && <ErrorNotice>{act.error}</ErrorNotice>}
      {live ? (
        <div className="approval-actions">
          <button
            type="button"
            className="pbtn primary"
            disabled={act.pending}
            onClick={() => void act.run("once")}
          >
            Allow once
          </button>
          <button
            type="button"
            className="pbtn"
            disabled={act.pending}
            onClick={() => void act.run("always")}
            title={`Never ask again for “${action?.type}”${host ? ` on ${host}` : ""}`}
          >
            Always allow{host ? ` on ${host}` : ""}
          </button>
          <button
            type="button"
            className="pbtn danger"
            disabled={act.pending}
            onClick={() => void act.run("deny")}
          >
            Deny
          </button>
        </div>
      ) : (
        <div className="reason">{decision ? DECIDED[decision] : "No longer pending"}</div>
      )}
    </div>
  );
}

function safeHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}
