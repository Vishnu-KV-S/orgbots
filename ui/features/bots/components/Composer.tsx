"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  type Attachment,
  type Bot,
  type BotMessage,
  listSkills,
  rawFileUrl,
  uploadFiles,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useResource } from "@/lib/hooks/useResource";
import { type SpeechRecognitionLike, speechRecognition } from "../lib/speech";
import { QUICK_PROMPTS } from "../lib/templates";
import { Avatar } from "./Avatar";

type Popup =
  | { kind: "slash"; query: string }
  | { kind: "mention"; query: string; at: number }
  | null;

/**
 * Where a task is given. Enter sends, Shift+Enter is a new line.
 *
 * `/` opens the prompt menu — quick prompts, then the organization's skills (a skill
 * named as `/name` in a message is loaded into the bot's prompt) — `@` mentions another
 * bot, the microphone dictates, and 📎 (or pasting an image, or dropping files on the box)
 * attaches files — uploaded into the team's drive at once, so a file that cannot be
 * stored says so before the message is sent. Ctrl/⌘+D starts and stops dictation, and
 * with the box empty, 📞 starts a voice chat; the bot
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
  onVoice,
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
  onSend: (text: string, attachments: Attachment[]) => Promise<boolean>;
  /** Start a voice chat (offered when the box is empty). */
  onVoice?: () => void;
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
  const skills = useResource(listSkills, { intervalMs: 30_000 });
  const [attached, setAttached] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => setAttached([]), [bot.id]);

  const attach = async (files: File[]) => {
    if (!files.length) return;
    const tooBig = files.find((f) => f.size > 10 * 1024 * 1024);
    if (tooBig) {
      setUploadError(`${tooBig.name} is over 10 MB`);
      return;
    }
    setUploadError(null);
    setUploading((n) => n + files.length);
    try {
      const stored = await uploadFiles(bot.id, files);
      setAttached((a) => [
        ...a,
        ...stored.files.map((f) => ({
          id: f.id,
          path: f.path,
          name: f.name,
          media_type: f.media_type,
          bytes: f.bytes,
          kind: f.kind,
        })),
      ]);
    } catch (cause) {
      setUploadError((cause as Error).message);
    } finally {
      setUploading((n) => n - files.length);
    }
  };

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
      const prompts = QUICK_PROMPTS.filter((p) => p.cmd.startsWith(q)).map((p) => ({
        key: p.cmd,
        label: `/${p.cmd}`,
        hint: p.hint,
        apply: () => {
          onDraft(p.text);
          setPopup(null);
        },
      }));
      const library = (skills.data?.skills ?? [])
        .filter((k) => k.name.includes(q) || k.title.toLowerCase().includes(q))
        .slice(0, 8)
        .map((k) => ({
          key: `skill:${k.id}`,
          label: `/${k.name}`,
          hint: `${k.status === "draft" ? "Draft skill · " : "Skill · "}${k.title || k.when}`,
          apply: () => {
            onDraft(`/${k.name} `);
            setPopup(null);
          },
        }));
      return [...prompts, ...library];
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
  }, [popup, bots, bot.id, draft, onDraft, skills.data]);

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
      setPopup({
        kind: "mention",
        query: mention[2],
        at: upto.length - mention[2].length - 1,
      });
      setSel(0);
      return;
    }
    setPopup(null);
  };

  const submit = () => {
    const text = draft.trim();
    if ((!text && attached.length === 0) || sending || uploading > 0) return;
    void onSend(text, attached).then((ok) => {
      if (ok) setAttached([]);
    });
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
      <div
        className={cx("composer", dragging && "dropping")}
        onDragOver={(e) => {
          if (Array.from(e.dataTransfer.types).includes("Files")) {
            e.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          if (!e.dataTransfer.files.length) return;
          e.preventDefault();
          setDragging(false);
          void attach(Array.from(e.dataTransfer.files));
        }}
      >
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
                {o.avatar ? <Avatar bot={o.avatar} size={22} /> : null}
                <span>{o.label}</span>
                <small>{o.hint}</small>
              </button>
            ))}
          </div>
        )}
        {replyTo && (
          <div className="replying">
            ↪ <span>Replying to: {replyTo.content}</span>
            <button
              type="button"
              className="ibtn"
              onClick={onCancelReply}
              aria-label="Cancel reply"
            >
              ✕
            </button>
          </div>
        )}
        {(attached.length > 0 || uploading > 0 || uploadError) && (
          <div className="attach-row">
            {attached.map((a) => (
              <span key={a.id} className="attach-chip" title={a.path}>
                {a.kind === "image" ? (
                  // eslint-disable-next-line @next/next/no-img-element -- a just-uploaded file
                  <img src={rawFileUrl(bot.id, a.id)} alt="" />
                ) : (
                  <span className="attach-kind">{a.kind === "text" ? "TXT" : a.kind}</span>
                )}
                <span className="attach-name">{a.name}</span>
                <button
                  type="button"
                  aria-label={`Remove ${a.name}`}
                  onClick={() => setAttached((list) => list.filter((x) => x.id !== a.id))}
                >
                  ✕
                </button>
              </span>
            ))}
            {uploading > 0 && <span className="attach-chip pending">Uploading…</span>}
            {uploadError && <span className="attach-error">{uploadError}</span>}
          </div>
        )}
        <textarea
          ref={ref}
          rows={1}
          value={draft}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData.files);
            if (files.length) {
              e.preventDefault();
              void attach(files);
            }
          }}
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
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "d" && canDictate) {
              e.preventDefault();
              dictate();
              return;
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
          <input
            ref={picker}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              const list = Array.from(e.target.files ?? []);
              e.target.value = "";
              void attach(list);
            }}
          />
          <button
            type="button"
            className="ibtn"
            onClick={() => picker.current?.click()}
            title="Attach files (or paste an image, or drop files here)"
            aria-label="Attach files"
          >
            📎
          </button>
          {canDictate && (
            <button
              type="button"
              className={cx("ibtn", listening && "on")}
              onClick={dictate}
              title={listening ? "Stop dictation (Ctrl+D)" : "Dictate (Ctrl+D)"}
              aria-label="Dictate"
            >
              🎙
            </button>
          )}
          {onVoice && canDictate && !draft.trim() && attached.length === 0 && (
            <button
              type="button"
              className="ibtn"
              onClick={onVoice}
              title="Start a voice chat"
              aria-label="Start a voice chat"
            >
              📞
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
            disabled={(!draft.trim() && attached.length === 0) || sending || uploading > 0}
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
