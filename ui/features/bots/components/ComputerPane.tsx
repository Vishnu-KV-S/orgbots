"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type HumanInput,
  computerStatus,
  resetComputer,
  screenshotUrl,
  sendInput,
  setController,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";

const VIEWPORT = { width: 1280, height: 800 };
const FRAME_MS = 900;

const SPECIAL_KEYS = new Set([
  "Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft",
  "ArrowRight", "Home", "End", "PageUp", "PageDown",
]);

/**
 * The bot's screen, live. All bots share one cloud computer and each has its own
 * screen, so this is *this* bot's tab.
 *
 * Watching is passive. "Take control" hands the screen to the person — the
 * computer then refuses the bot's actions until control is returned — and only
 * then do clicks and keys on the image go through. That makes a login, a 2FA code
 * or a CAPTCHA something the person does themselves, in the bot's own browser,
 * with the bot's session.
 */
export function ComputerPane({ bot }: { bot: Bot }) {
  const [frame, setFrame] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [broken, setBroken] = useState(false);
  const [controller, setCtl] = useState<"bot" | "human">("bot");
  const [url, setUrl] = useState("");
  const [editingUrl, setEditingUrl] = useState(false);
  const [typing, setTyping] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const screen = useRef<HTMLDivElement>(null);
  const human = controller === "human";

  // Status: who holds the screen, where it is.
  const refreshStatus = useCallback(async () => {
    try {
      const status = await computerStatus();
      setReachable(status.reachable);
      const mine = status.screens?.find((s) => s.screen_id === bot.id);
      if (mine) {
        setCtl(mine.controller === "human" ? "human" : "bot");
        if (!editingUrl) setUrl(mine.url);
      }
    } catch {
      setReachable(false);
    }
  }, [bot.id, editingUrl]);

  useEffect(() => {
    void refreshStatus();
    const t = setInterval(() => void refreshStatus(), 2500);
    return () => clearInterval(t);
  }, [refreshStatus]);

  useEffect(() => {
    setLoaded(false);
    setBroken(false);
    setFrame(Date.now());
  }, [bot.id]);

  // The next frame is requested only after this one has loaded or failed, so a
  // slow computer is never asked for frames faster than it can render them.
  const next = useCallback((delay: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setFrame(Date.now()), delay);
  }, []);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const input = async (event: HumanInput) => {
    setError(null);
    try {
      const result = await sendInput(bot.id, event);
      if (result.url) setUrl(result.url);
      next(150);
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const toggleControl = async () => {
    setError(null);
    const target = human ? "bot" : "human";
    try {
      await setController(bot.id, target);
      setCtl(target);
      if (target === "human") screen.current?.focus();
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  const onClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!human) return;
    const img = e.currentTarget.querySelector("img");
    if (!img) return;
    const rect = img.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * VIEWPORT.width;
    const y = ((e.clientY - rect.top) / rect.height) * VIEWPORT.height;
    screen.current?.focus();
    void input({ kind: "click", x: Math.round(x), y: Math.round(y) });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!human || e.metaKey || e.ctrlKey || e.altKey) return;
    if (SPECIAL_KEYS.has(e.key)) {
      e.preventDefault();
      void input({ kind: "key", key: e.key });
    } else if (e.key.length === 1) {
      e.preventDefault();
      void input({ kind: "type", text: e.key });
    }
  };

  return (
    <div>
      <div className="screen-bar">
        <button
          type="button"
          className="ibtn"
          disabled={!human}
          onClick={() => void input({ kind: "back" })}
          aria-label="Back"
        >
          ←
        </button>
        <button
          type="button"
          className="ibtn"
          disabled={!human}
          onClick={() => void input({ kind: "reload" })}
          aria-label="Reload"
        >
          ↻
        </button>
        <input
          className="urlbox"
          value={url}
          disabled={!human}
          onFocus={() => setEditingUrl(true)}
          onBlur={() => setEditingUrl(false)}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && url.trim()) {
              void input({ kind: "navigate", url: url.trim() });
              (e.target as HTMLInputElement).blur();
            }
          }}
          aria-label="Address"
          placeholder="about:blank"
        />
      </div>

      <div
        ref={screen}
        className={cx("screen", human && "human")}
        tabIndex={human ? 0 : -1}
        onClick={onClick}
        onKeyDown={onKeyDown}
        onWheel={(e) => {
          if (human) void input({ kind: "scroll", dy: e.deltaY });
        }}
        aria-label={human ? "Bot screen — you have control" : "Bot screen"}
      >
        {reachable !== false && (
          // eslint-disable-next-line @next/next/no-img-element -- a live JPEG stream
          <img
            src={screenshotUrl(bot.id, frame)}
            alt={`${bot.name}'s screen`}
            draggable={false}
            onLoad={() => {
              setLoaded(true);
              setBroken(false);
              next(FRAME_MS);
            }}
            onError={() => {
              setBroken(true);
              next(FRAME_MS * 3);
            }}
          />
        )}
        {(reachable === false || (broken && !loaded)) && (
          <div className="screen-overlay">
            The cloud computer isn&apos;t running.
            <br />
            Start it with <code>python -m runtime.computer.main</code>
          </div>
        )}
        {loaded && <span className="screen-badge">{human ? "You have control" : "Live"}</span>}
      </div>

      {human ? (
        <>
          <p className="screen-help">
            Click on the screen to interact; keystrokes go to the page while it&apos;s focused.
            Paste or type longer text below. Hand control back when you&apos;re done.
          </p>
          <div className="typebar">
            <input
              className="urlbox"
              style={{ fontFamily: "var(--sans)" }}
              value={typing}
              placeholder="Type text into the page…"
              onChange={(e) => setTyping(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && typing) {
                  void input({ kind: "type", text: typing });
                  setTyping("");
                }
              }}
            />
            <button
              type="button"
              className="pbtn"
              disabled={!typing}
              onClick={() => {
                void input({ kind: "type", text: typing });
                setTyping("");
              }}
            >
              Type
            </button>
            <button type="button" className="pbtn" onClick={() => void input({ kind: "key", key: "Enter" })}>
              ⏎
            </button>
          </div>
        </>
      ) : (
        <p className="screen-help">
          Watching {bot.name}&apos;s screen. Take control to sign in, solve a CAPTCHA or finish
          a step yourself — the bot pauses until you hand it back.
        </p>
      )}

      {error && <ErrorNotice>{error}</ErrorNotice>}

      <div className="control-row">
        <button type="button" className={cx("pbtn", !human && "primary")} onClick={() => void toggleControl()}>
          {human ? "Return control to bot" : "Take control"}
        </button>
        <button
          type="button"
          className="pbtn"
          onClick={() => {
            if (window.confirm("Restart the cloud computer? Open pages close; sign-ins are kept.")) {
              void resetComputer().then(
                () => next(1500),
                (cause: Error) => setError(cause.message),
              );
            }
          }}
        >
          Recover computer
        </button>
      </div>
    </div>
  );
}
