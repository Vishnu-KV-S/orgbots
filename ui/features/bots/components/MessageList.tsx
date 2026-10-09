"use client";

import { useEffect, useMemo, useState } from "react";
import {
  type Attachment,
  type Bot,
  type BotMessage,
  rawFileUrl,
  reactToBotMessage,
} from "@/lib/api/bots";
import { useMe } from "../lib/me";
import { canSpeak, speak, stopSpeaking } from "../lib/speech";
import { ACTION_ICON, OPENS_FILE, RichText, describeAction } from "../lib/text";
import { ApprovalCard } from "./ApprovalCard";
import { CredentialCard } from "./CredentialCard";
import { Screenshot } from "./Screenshot";
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

/** The person's reactions, kept on the server: each message brings its own, and a
 * toggle keeps what the server answered (the transcript only appends new rows). */
function useReactions(botId: string) {
  const [overrides, setOverrides] = useState<Record<string, string[]>>({});
  useEffect(() => setOverrides({}), [botId]);
  const of = (m: BotMessage) => overrides[m.id] ?? m.reactions ?? [];
  const react = async (m: BotMessage, emoji: string) => {
    const on = !of(m).includes(emoji);
    try {
      const result = await reactToBotMessage(botId, m.id, emoji, on);
      setOverrides((o) => ({ ...o, [m.id]: result.reactions }));
    } catch {
      /* a reaction that did not save just does not show */
    }
  };
  return { of, react };
}

function WorkBlock({
  botId,
  steps,
  live,
  onOpenFile,
}: {
  botId: string;
  steps: BotMessage[];
  live: boolean;
  onOpenFile: (path: string) => void;
}) {
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
            const file =
              s.payload.ok && OPENS_FILE.has(action?.type ?? "")
                ? (action?.to ?? action?.path)
                : undefined;
            return (
              <div key={s.id} className="stepline">
                <span className="ico">{ACTION_ICON[action?.type ?? ""] ?? "•"}</span>
                <div>
                  <div className="what">
                    {file ? (
                      <button
                        type="button"
                        className="linklike"
                        title="Open in Files"
                        onClick={() => onOpenFile(file)}
                      >
                        {describeAction(action)}
                      </button>
                    ) : (
                      describeAction(action)
                    )}
                  </div>
                  {s.content && <div className="why">{s.content}</div>}
                  {s.payload.ok === false && s.payload.error && (
                    <div className="fail">{s.payload.error}</div>
                  )}
                  {s.payload.note && <div className="why">“{s.payload.note}”</div>}
                  {s.payload.screenshot_id && (
                    <Screenshot
                      botId={botId}
                      screenshotId={s.payload.screenshot_id}
                      caption={describeAction(action)}
                      size="sm"
                    />
                  )}
                  {s.payload.url && <div className="where">{s.payload.url}</div>}
                  {s.payload.output && (
                    <pre className="cmd-out">
                      {s.payload.timed_out
                        ? "(timed out) "
                        : s.payload.exit_code !== undefined && s.payload.exit_code !== 0
                          ? `(exit ${s.payload.exit_code}) `
                          : ""}
                      {s.payload.output}
                    </pre>
                  )}
                  {s.payload.schedule && (
                    <div className="where">
                      {s.payload.schedule}
                      {s.payload.active === false ? " · paused" : ""}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** A message's files: images as thumbnails (click for full size), the rest as chips
 * that open the file in the Files pane, where the bots read it too. */
function Attachments({
  bot,
  files,
  onOpen,
}: {
  bot: Bot;
  files: Attachment[];
  onOpen: (path: string) => void;
}) {
  return (
    <div className="msg-attachments">
      {files.map((a) =>
        a.kind === "image" ? (
          <a key={a.id} href={rawFileUrl(bot.id, a.id)} target="_blank" rel="noreferrer">
            {/* eslint-disable-next-line @next/next/no-img-element -- a file from the drive */}
            <img src={rawFileUrl(bot.id, a.id)} alt={a.name} />
          </a>
        ) : (
          <button
            key={a.id}
            type="button"
            className="attach-chip"
            title={`Open ${a.path} in Files`}
            onClick={() => onOpen(a.path)}
          >
            <span className="attach-kind">{a.kind === "text" ? "TXT" : a.kind}</span>
            <span className="attach-name">{a.name}</span>
          </button>
        ),
      )}
    </div>
  );
}

/** A reply read aloud — a voice memo, with the text beside it as its transcript. */
function VoiceMemo({ text }: { text: string }) {
  const [playing, setPlaying] = useState(false);
  if (!canSpeak()) return null;
  return (
    <button
      type="button"
      aria-label={playing ? "Stop voice memo" : "Play voice memo"}
      onClick={() => {
        if (playing) {
          stopSpeaking();
          setPlaying(false);
          return;
        }
        stopSpeaking();
        setPlaying(true);
        void speak(text).then(() => setPlaying(false));
      }}
    >
      {playing ? "■ Stop" : "▶ Play"}
    </button>
  );
}

/** A demonstration's message: the goal, with the recorded steps folded away — they
 * are for the bot to write up, and a long recording would bury the conversation. */
function Demonstration({ message }: { message: BotMessage }) {
  const [open, setOpen] = useState(false);
  const recorded =
    message.content.split("<<<RECORDING")[1]?.split("RECORDING>>>")[0]?.trim() ?? "";
  return (
    <>
      <div>Showed how to: {message.payload.demonstration}</div>
      <button type="button" className="linklike demo-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide" : "Show"} {message.payload.steps ?? 0} recorded steps
      </button>
      {open && <pre className="demo-steps">{recorded}</pre>}
    </>
  );
}

export function MessageList({
  bot,
  messages,
  pending,
  asking,
  working,
  onReply,
  onDecided,
  onOpenFile,
}: {
  bot: Bot;
  messages: BotMessage[];
  pending: Set<string>;
  asking: Set<string>;
  working: boolean;
  onReply: (m: BotMessage) => void;
  onDecided: () => void;
  onOpenFile: (path: string) => void;
}) {
  const me = useMe();
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
  const answered = useMemo(() => {
    const out = new Map<string, { kind: string; saved: boolean }>();
    for (const m of messages) {
      const rid = m.payload.credential_request_id;
      if (m.role === "system" && rid && m.payload.decision) {
        out.set(rid, { kind: m.payload.decision, saved: m.payload.saved === true });
      }
    }
    return out;
  }, [messages]);
  const { of: reactionsOf, react } = useReactions(bot.id);

  return (
    <>
      {items.map((item, index) => {
        if (item.kind === "work") {
          return (
            <WorkBlock
              key={item.id}
              botId={bot.id}
              steps={item.steps}
              live={working && index === items.length - 1}
              onOpenFile={onOpenFile}
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
        if (m.role === "credentials") {
          const rid = m.payload.credential_request_id ?? "";
          return (
            <CredentialCard
              key={m.id}
              botId={bot.id}
              botName={bot.name}
              message={m}
              live={asking.has(rid)}
              decision={answered.get(rid) ?? null}
              onDone={onDecided}
              shared={bot.visibility === "team" && bot.owner_member_id !== null}
            />
          );
        }
        if (m.role === "system") {
          // Decision notes are shown on the card they decide.
          if (m.payload.pending_id || m.payload.credential_request_id) return null;
          return (
            <div key={m.id} className={m.payload.voice_call ? "sysline callcard" : "sysline"}>
              {m.payload.voice_call ? "📞 " : ""}
              {m.content}
              {m.payload.screenshot_id && (
                <Screenshot botId={bot.id} screenshotId={m.payload.screenshot_id} />
              )}
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
              {isUser && m.payload.from && m.payload.from.member_id !== me?.id && (
                <div className="msg-from">{m.payload.from.name}</div>
              )}
              {m.payload.demonstration && <div className="from-bot">🎓 Demonstration</div>}
              {isUser && m.payload.voice && <div className="from-bot">🎙 Said in a voice chat</div>}
              {!isUser && m.payload.group_id && (
                <div className="gm-author">
                  Posted in a group
                  {m.payload.handed_to?.length
                    ? ` · handed to ${m.payload.handed_to.join(", ")}`
                    : ""}
                </div>
              )}
              {m.payload.routine && (
                <div className="from-bot">
                  {m.payload.trigger === "test"
                    ? "⏰ Test run"
                    : m.payload.trigger === "event"
                      ? "⚡ Event"
                      : "⏰ Routine"}{" "}
                  · {m.payload.routine}
                </div>
              )}
              <div className="bubble" style={{ maxWidth: "100%" }}>
                {quoted && <div className="quote">↪ {quoted.content}</div>}
                {isUser ? (
                  m.payload.demonstration ? (
                    <Demonstration message={m} />
                  ) : (
                    m.content
                  )
                ) : (
                  <RichText text={m.content} />
                )}
                {m.payload.attachments && m.payload.attachments.length > 0 && (
                  <Attachments bot={bot} files={m.payload.attachments} onOpen={onOpenFile} />
                )}
              </div>
              {!isUser && m.payload.screenshot_id && (
                <Screenshot
                  botId={bot.id}
                  screenshotId={m.payload.screenshot_id}
                  caption={`${bot.name}'s screen`}
                />
              )}
              {reactionsOf(m).length > 0 && (
                <div
                  className="reactions"
                  style={{ justifyContent: isUser ? "flex-end" : "start" }}
                >
                  {reactionsOf(m).map((emoji) => (
                    <span key={emoji} className="reaction">
                      {emoji}
                    </span>
                  ))}
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
                  <VoiceMemo text={m.content} />
                  <button type="button" onClick={() => void react(m, "👍")} aria-label="Good">
                    👍
                  </button>
                  <button type="button" onClick={() => void react(m, "👎")} aria-label="Bad">
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
