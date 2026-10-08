"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Bot, BotMessage } from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { QUICK_PROMPTS } from "../lib/templates";
import { Avatar } from "./Avatar";

type Popup =
  | { kind: "slash"; query: string }
  | { kind: "mention"; query: string; at: number }
  | null;

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onend: (() => void) | null;
}

function speechRecognition(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null) as
    | (new () => SpeechRecognitionLike)
    | null;
}

/**
 * Where a task is given. Enter sends, Shift+Enter is a new line.
 *
 * `/` opens the prompt menu, `@` mentions another bot, the microphone dictates
 * (where the browser has speech recognition), and while the bot is working the
 * send button becomes Stop — a new message also redirects a working bot, which is
 * the runtime's "turn" mechanism on the other side.
 */
export function Composer({
  bot,
  bots,
  working,
  replyTo,
  onCancelReply,
  onSend,
  onStop,
  sending,
  draft,
  onDraft,
}: {
  bot: Bot;
  bots: Bot[];
  working: boolean;
  replyTo: BotMessage | null;
  onCancelReply: () => void;
  onSend: (text: string) => void;
  onStop: () => void;
  sending: boolean;
  draft: string;
  onDraft: (value: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [popup, setPopup] = useState<Popup>(null);
  const [sel, setSel] = useState(0);
  const [listening, setListening] = useState(false);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const canDictate = speechRecognition() !== null;

  useEffect(() => {
    ref.current?.focus();
  }, [bot.id, replyTo]);

  // Grow with the content, up to the CSS max-height.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [draft]);

  const options = useMemo((): {
    key: string;
    label: string;
    hint: string;
    avatar?: Bot;
    apply: () => void;
  }[] => {
    if (!popup) return [];
    const q = popup.query.toLowerCase();
    if (popup.kind === "slash") {
      return QUICK_PROMPTS.filter((p) => p.cmd.startsWith(q)).map((p) => ({
        key: p.cmd,
        label: `/${p.cmd}`,
        hint: p.hint,
        apply: () => {
          onDraft(p.text);
          setPopup(null);
        },
      }));
    }
    return bots
      .filter((b) => b.id !== bot.id && b.name.toLowerCase().includes(q))
      .slice(0, 8)
      .map((b) => ({
        key: b.id,
        label: b.name,
        hint: b.label,
        avatar: b,
        apply: () => {
          const before = draft.slice(0, popup.at);
          const after = draft.slice(popup.at + 1 + popup.query.length);
          onDraft(`${before}@${b.name} ${after}`);
          setPopup(null);
        },
      }));
  }, [popup, bots, bot.id, draft, onDraft]);

  const update = (value: string, caret: number) => {
    onDraft(value);
    if (value.startsWith("/") && !value.includes(" ")) {
      setPopup({ kind: "slash", query: value.slice(1) });
      setSel(0);
      return;
    }
    const upto = value.slice(0, caret);
    const mention = /(^|\s)@([\w-]*)$/.exec(upto);
    if (mention) {
      setPopup({ kind: "mention", query: mention[2], at: upto.length - mention[2].length - 1 });
      setSel(0);
      return;
    }
    setPopup(null);
  };

  const submit = () => {
    const text = draft.trim();
    if (!text || sending) return;
    onSend(text);
  };

  const dictate = () => {
    const Recognition = speechRecognition();
    if (!Recognition) return;
    if (listening) {
      recognition.current?.stop();
      return;
    }
    const r = new Recognition();
    r.continuous = true;
    r.interimResults = false;
    r.lang = navigator.language || "en-US";
    const base = draft ? `${draft.trimEnd()} ` : "";
    let heard = "";
    r.onresult = (e) => {
      heard = Array.from(e.results)
        .map((result) => result[0].transcript)
        .join(" ");
      onDraft(base + heard);
    };
    r.onend = () => setListening(false);
    recognition.current = r;
    setListening(true);
    r.start();
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        {popup && options.length > 0 && (
          <div className="popmenu" role="listbox">
            <div className="popmenu-label">{popup.kind === "slash" ? "Prompts" : "Bots"}</div>
            {options.map((o, i) => (
              <button
                key={o.key}
                type="button"
                className={cx("popitem", i === sel && "sel")}
                onMouseDown={(e) => {
                  e.preventDefault();
                  o.apply();
                }}
              >
                {o.avatar ? (
                  <Avatar name={o.avatar.name} avatar={o.avatar.avatar} size="sm" />
                ) : null}
                <span>{o.label}</span>
                <small>{o.hint}</small>
              </button>
            ))}
          </div>
        )}
        {replyTo && (
          <div className="replying">
            ↪ <span>Replying to: {replyTo.content}</span>
            <button type="button" className="ibtn" onClick={onCancelReply} aria-label="Cancel reply">
              ✕
            </button>
          </div>
        )}
        <textarea
          ref={ref}
          rows={1}
          value={draft}
          placeholder={`Message ${bot.name} — / for prompts, @ to mention`}
          onChange={(e) => update(e.target.value, e.target.selectionStart)}
          onKeyDown={(e) => {
            if (popup && options.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSel((s) => (s + 1) % options.length);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setSel((s) => (s - 1 + options.length) % options.length);
                return;
              }
              if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                options[sel]?.apply();
                return;
              }
              if (e.key === "Escape") {
                setPopup(null);
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-row">
          <span className="composer-hint">
            {working ? "Working — send a message to redirect, or stop." : "Enter to send"}
          </span>
          {canDictate && (
            <button
              type="button"
              className={cx("ibtn", listening && "on")}
              onClick={dictate}
              title={listening ? "Stop dictation" : "Dictate"}
              aria-label="Dictate"
            >
              🎙
            </button>
          )}
          {working && (
            <button
              type="button"
              className="ibtn stop"
              onClick={onStop}
              title="Stop now"
              aria-label="Stop now"
            >
              ■
            </button>
          )}
          <button
            type="button"
            className="ibtn send"
            onClick={submit}
            disabled={!draft.trim() || sending}
            title="Send"
            aria-label="Send"
          >
            ↑
          </button>
        </div>
      </div>
    </div>
  );
}
