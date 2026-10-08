"use client";

import { useEffect, useMemo, useState } from "react";
import type { Bot, BotMessage } from "@/lib/api/bots";
import { ACTION_ICON, RichText, describeAction } from "../lib/text";
import { ApprovalCard } from "./ApprovalCard";
import { Avatar } from "./Avatar";

type Item =
  | { kind: "message"; message: BotMessage }
  | { kind: "work"; id: string; steps: BotMessage[] };

/** Consecutive activity rows fold into one "worked" block. */
function group(messages: BotMessage[]): Item[] {
  const items: Item[] = [];
  for (const m of messages) {
    if (m.role === "activity") {
      const last = items[items.length - 1];
      if (last?.kind === "work") last.steps.push(m);
      else items.push({ kind: "work", id: m.id, steps: [m] });
    } else {
      items.push({ kind: "message", message: m });
    }
  }
  return items;
}

const REACTIONS_KEY = "bot-reactions";

function useReactions() {
  const [reactions, setReactions] = useState<Record<string, string>>({});
  useEffect(() => {
    try {
      setReactions(JSON.parse(localStorage.getItem(REACTIONS_KEY) ?? "{}"));
    } catch {
      /* storage unavailable: reactions just won't persist */
    }
  }, []);
  const react = (id: string, emoji: string) =>
    setReactions((prev) => {
      const next = { ...prev };
      if (next[id] === emoji) delete next[id];
      else next[id] = emoji;
      try {
        localStorage.setItem(REACTIONS_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  return { reactions, react };
}

function WorkBlock({ steps, live }: { steps: BotMessage[]; live: boolean }) {
  const [open, setOpen] = useState(false);
  const expanded = open || live;
  const latest = steps[steps.length - 1];
  const failures = steps.filter((s) => s.payload.ok === false).length;
  return (
    <div className="work">
      <button type="button" className="work-head" onClick={() => setOpen((o) => !o)}>
        {live ? <span className="spinner" /> : <span>✓</span>}
        <span>
          {live ? describeAction(latest.payload.action) || "Working" : "Worked"} · {steps.length}{" "}
          step{steps.length === 1 ? "" : "s"}
          {failures > 0 && ` · ${failures} retried`}
        </span>
        <span className="chev">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <div className="steps">
          {(live ? steps.slice(-8) : steps).map((s) => {
            const action = s.payload.action;
            return (
              <div key={s.id} className="stepline">
                <span className="ico">{ACTION_ICON[action?.type ?? ""] ?? "•"}</span>
                <div>
                  <div className="what">{describeAction(action)}</div>
                  {s.content && <div className="why">{s.content}</div>}
                  {s.payload.ok === false && s.payload.error && (
                    <div className="fail">{s.payload.error}</div>
                  )}
                  {s.payload.note && <div className="why">“{s.payload.note}”</div>}
                  {s.payload.url && <div className="where">{s.payload.url}</div>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function MessageList({
  bot,
  messages,
  pending,
  working,
  onReply,
  onDecided,
}: {
  bot: Bot;
  messages: BotMessage[];
  pending: Set<string>;
  working: boolean;
  onReply: (m: BotMessage) => void;
  onDecided: () => void;
}) {
  const items = useMemo(() => group(messages), [messages]);
  const byId = useMemo(() => new Map(messages.map((m) => [m.id, m])), [messages]);
  const decisions = useMemo(() => {
    const out = new Map<string, string>();
    for (const m of messages) {
      if (m.role === "system" && m.payload.pending_id && m.payload.decision) {
        out.set(m.payload.pending_id, m.payload.decision);
      }
    }
    return out;
  }, [messages]);
  const { reactions, react } = useReactions();

  return (
    <>
      {items.map((item, index) => {
        if (item.kind === "work") {
          return (
            <WorkBlock
              key={item.id}
              steps={item.steps}
              live={working && index === items.length - 1}
            />
          );
        }
        const m = item.message;
        if (m.role === "approval") {
          const pid = m.payload.pending_id ?? "";
          return (
            <ApprovalCard
              key={m.id}
              botId={bot.id}
              message={m}
              live={pending.has(pid)}
              decision={decisions.get(pid) ?? null}
              onDecided={onDecided}
            />
          );
        }
        if (m.role === "system") {
          // Decision notes are shown on the card they decide.
          if (m.payload.pending_id) return null;
          return (
            <div key={m.id} className="sysline">
              {m.content}
            </div>
          );
        }
        if (m.role === "error") {
          return (
            <div key={m.id} className="errline" role="alert">
              {m.content}
            </div>
          );
        }
        const quoted = m.reply_to ? byId.get(m.reply_to) : undefined;
        const isUser = m.role === "user";
        return (
          <div key={m.id} className={isUser ? "msg user" : "msg bot"}>
            {!isUser && <Avatar bot={bot} size={24} />}
            <div style={{ minWidth: 0, maxWidth: isUser ? "82%" : "100%" }}>
              {m.payload.from_bot_name && (
                <div className="from-bot">From {m.payload.from_bot_name} (bot)</div>
              )}
              <div className="bubble" style={{ maxWidth: "100%" }}>
                {quoted && <div className="quote">↪ {quoted.content}</div>}
                {isUser ? m.content : <RichText text={m.content} />}
              </div>
              {reactions[m.id] && (
                <div
                  className="reactions"
                  style={{ justifyContent: isUser ? "flex-end" : "start" }}
                >
                  <span className="reaction">{reactions[m.id]}</span>
                </div>
              )}
            </div>
            <div className="msg-actions">
              <button type="button" onClick={() => onReply(m)}>
                Reply
              </button>
              <button type="button" onClick={() => void navigator.clipboard?.writeText(m.content)}>
                Copy
              </button>
              {!isUser && (
                <>
                  <button type="button" onClick={() => react(m.id, "👍")} aria-label="Good">
                    👍
                  </button>
                  <button type="button" onClick={() => react(m.id, "👎")} aria-label="Bad">
                    👎
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
      {working && items[items.length - 1]?.kind !== "work" && (
        <div className="work">
          <div className="work-head">
            <span className="spinner" />
            <span>Thinking…</span>
          </div>
        </div>
      )}
    </>
  );
}
