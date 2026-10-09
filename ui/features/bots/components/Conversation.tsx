"use client";

import {
  ArrowLeftIcon,
  ComputerDesktopIcon,
  FolderIcon,
  InformationCircleIcon,
  SparklesIcon,
  Squares2X2Icon,
} from "@heroicons/react/24/outline";
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Attachment,
  type Bot,
  type BotMessage,
  markRead,
  sendMessage,
  stopBot,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useConversation } from "../hooks/useConversation";
import { moodOfConversation } from "../avatar";
import { Avatar } from "./Avatar";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { VoiceCall } from "./VoiceCall";

const STARTERS = [
  "Find the three best-reviewed robot vacuums under $300 and compare them.",
  "Open news.ycombinator.com and summarize the top 5 stories.",
  "Look up the opening hours of the nearest public library.",
];

export type Pane = "computer" | "files" | "skills" | "apps" | "details" | null;

export function Conversation({
  bot,
  bots,
  pane,
  onPane,
  onBack,
  onChanged,
  onOpenFile,
}: {
  bot: Bot;
  bots: Bot[];
  pane: Pane;
  onPane: (pane: Pane) => void;
  onBack: () => void;
  onChanged: () => void;
  /** Open a team file, named by path, in the Files pane. */
  onOpenFile: (path: string) => void;
}) {
  const convo = useConversation(bot.id);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [replyTo, setReplyTo] = useState<BotMessage | null>(null);
  const [sending, setSending] = useState(false);
  const [calling, setCalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const pinnedToBottom = useRef(true);
  const draft = drafts[bot.id] ?? "";
  const setDraft = useCallback(
    (value: string) => setDrafts((d) => ({ ...d, [bot.id]: value })),
    [bot.id],
  );

  // Reading the conversation marks it read.
  useEffect(() => {
    if (bot.unread) void markRead(bot.id).then(onChanged, () => undefined);
  }, [bot.id, bot.unread, convo.messages.length, onChanged]);

  useEffect(() => {
    setReplyTo(null);
    setError(null);
    pinnedToBottom.current = true;
  }, [bot.id]);

  // Follow new messages, unless the person has scrolled up to read.
  useEffect(() => {
    const el = scroller.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [convo.messages, convo.working]);

  const send = async (
    text: string,
    attachments: Attachment[] = [],
    voice = false,
  ): Promise<boolean> => {
    setSending(true);
    setError(null);
    try {
      const sent = await sendMessage(
        bot.id,
        text,
        replyTo?.id,
        attachments.map((a) => a.id),
        { voice },
      );
      setDraft("");
      setReplyTo(null);
      pinnedToBottom.current = true;
      if (!sent.admitted && sent.refusal_reason) setError(sent.refusal_reason);
      convo.poke();
      onChanged();
      return true;
    } catch (cause) {
      setError((cause as Error).message);
      return false;
    } finally {
      setSending(false);
    }
  };

  const stop = async () => {
    try {
      await stopBot(bot.id);
      convo.poke();
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const mood = moodOfConversation(convo.messages, {
    working: convo.working,
    pending: convo.pending.size + convo.asking.size,
  });
  const status = convo.working
    ? "Working…"
    : convo.asking.size > 0
      ? "Waiting for you to sign in"
      : convo.pending.size > 0
        ? "Waiting for your approval"
        : bot.label || bot.description || "Ready";

  return (
    <section className="convo" aria-label={`Conversation with ${bot.name}`}>
      <header className="convo-head">
        <button type="button" className="ibtn mobile-only" onClick={onBack} aria-label="Back">
          <ArrowLeftIcon />
        </button>
        <Avatar bot={bot} mood={mood} size={52} live />
        <div className="convo-title">
          <h2>{bot.name}</h2>
          <p>{status}</p>
        </div>
        <button
          type="button"
          className={cx("tab", pane === "computer" && "on")}
          onClick={() => onPane(pane === "computer" ? null : "computer")}
          title="Watch or take control of this bot's screen"
        >
          <ComputerDesktopIcon /> <span className="tab-label">Computer</span>
        </button>
        <button
          type="button"
          className={cx("tab", pane === "files" && "on")}
          onClick={() => onPane(pane === "files" ? null : "files")}
          title="The files this bot's team shares"
        >
          <FolderIcon /> <span className="tab-label">Files</span>
        </button>
        <button
          type="button"
          className={cx("tab", pane === "skills" && "on")}
          onClick={() => onPane(pane === "skills" ? null : "skills")}
          title="How-tos every bot can follow"
        >
          <SparklesIcon /> <span className="tab-label">Skills</span>
        </button>
        <button
          type="button"
          className={cx("tab", pane === "apps" && "on")}
          onClick={() => onPane(pane === "apps" ? null : "apps")}
          title="Apps every bot can call directly"
        >
          <Squares2X2Icon /> <span className="tab-label">Apps</span>
        </button>
        <button
          type="button"
          className={cx("tab", pane === "details" && "on")}
          onClick={() => onPane(pane === "details" ? null : "details")}
          title="Who this bot is, its routines, memory and rules"
        >
          <InformationCircleIcon /> <span className="tab-label">Details</span>
        </button>
      </header>

      <div
        className="convo-scroll"
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <div className="convo-inner">
          {convo.loaded && convo.messages.length === 0 && (
            <div className="welcome" style={{ padding: "60px 0" }}>
              <Avatar bot={bot} mood={mood} size={200} live />
              <h1>{bot.name}</h1>
              <p>
                {bot.description ||
                  "Give me a task in plain words. I'll use my browser to do it, show you each step, and ask before anything consequential."}
              </p>
              <div className="starter-chips">
                {STARTERS.map((s) => (
                  <button key={s} type="button" className="chipbtn" onClick={() => setDraft(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          <MessageList
            bot={bot}
            messages={convo.messages}
            pending={convo.pending}
            asking={convo.asking}
            working={convo.working}
            onReply={setReplyTo}
            onOpenFile={onOpenFile}
            onDecided={() => {
              convo.poke();
              onChanged();
            }}
          />
          {convo.error && !convo.loaded && <ErrorNotice>{convo.error}</ErrorNotice>}
          {error && <ErrorNotice>{error}</ErrorNotice>}
        </div>
      </div>

      {calling && (
        <VoiceCall
          bot={bot}
          messages={convo.messages}
          working={convo.working}
          onSend={(text) => send(text, [], true)}
          onEnd={() => {
            setCalling(false);
            convo.poke();
          }}
        />
      )}
      <Composer
        bot={bot}
        bots={bots}
        working={convo.working}
        replyTo={replyTo}
        onCancelReply={() => setReplyTo(null)}
        onSend={send}
        onVoice={() => setCalling(true)}
        onStop={() => void stop()}
        sending={sending}
        draft={draft}
        onDraft={setDraft}
      />
    </section>
  );
}
