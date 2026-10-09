"use client";

import {
  AcademicCapIcon,
  ArrowLeftIcon,
  ArrowPathIcon,
  ArrowTurnDownLeftIcon,
} from "@heroicons/react/24/outline";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type HumanInput,
  type MouseButton,
  type Teaching,
  computerStatus,
  desktopSocket,
  resetComputer,
  sendInputs,
  setController,
  showScreen,
  startTeaching,
  stopTeaching,
  streamUrl,
  teachingStatus,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { type DesktopState, DesktopView } from "./DesktopView";
import { inputQueue, readFrames } from "./remoteScreen";
import { WorkspacePane } from "./WorkspacePane";

/** Until the first frame says otherwise: the bot's screen is as big as its window. */
const DEFAULT_SIZE = { width: 1280, height: 800 };
const BUTTONS: Record<number, MouseButton> = { 0: "left", 1: "middle", 2: "right" };
const DOUBLE_CLICK_MS = 500;
const PASTE_MAX = 20_000;
const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** The key to hold down on the bot's computer, or null for one that has none. */
function remoteKey(e: React.KeyboardEvent): string | null {
  if (e.nativeEvent.isComposing || ["Unidentified", "Process", "Dead"].includes(e.key)) {
    return null;
  }
  // The bot's computer is Linux: ⌘A on a Mac means what Ctrl+A means there.
  if (IS_MAC && e.key === "Meta") return "Control";
  return e.key;
}

function isPaste(e: React.KeyboardEvent): boolean {
  return ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") || (e.shiftKey && e.key === "Insert");
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });

/**
 * The bot's screen, live. All bots share one cloud computer and each has its own
 * screen, so this is *this* bot's tab.
 *
 * Watching is passive. "Take control" hands the screen to the person — the
 * computer then refuses the bot's actions until control is returned — and from then
 * on the screen is theirs the way a remote desktop is: the picture is a live
 * stream, and the pointer's every movement, each press and release, the wheel and
 * every key down and up go through as they happen, in order and at the pace they
 * were made. Hover menus, drags, sliders, double and right clicks, shortcuts and
 * pasting from the person's own clipboard all work, so a login, a 2FA code or a
 * CAPTCHA is something the person does themselves, in the bot's own browser, with the
 * bot's session.
 *
 * "Teach a task" is the same takeover, recorded: the person does the task while the
 * computer writes down each click, field and page (never a typed password), and Stop
 * hands the recording to the bot, which writes it up as a draft skill.
 *
 * On a computer with a desktop (`docker/computer`) the pane opens on the **Desktop**:
 * the computer's own screen, with this bot's Chrome window brought to the front — the
 * browser's tabs, address bar and menus, the way GrokBot shows a bot's computer.
 * Taking control there hands the person the desktop's mouse and keyboard
 * (`DesktopView`). **Page** is the view above — only this bot's page, streamed from
 * the browser — which a recording uses, because it is where each input is written down.
 */
export function ComputerPane({ bot }: { bot: Bot }) {
  const [view, setView] = useState<"screen" | "workspace">("screen");
  return (
    <div>
      <div className="seg subtabs" role="tablist" aria-label="Computer">
        <button
          type="button"
          role="tab"
          aria-selected={view === "screen"}
          className={cx(view === "screen" && "on")}
          onClick={() => setView("screen")}
        >
          Screen
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "workspace"}
          className={cx(view === "workspace" && "on")}
          onClick={() => setView("workspace")}
        >
          Workspace &amp; terminal
        </button>
      </div>
      {view === "screen" ? <Screen bot={bot} /> : <WorkspacePane botId={bot.id} />}
    </div>
  );
}

function Screen({ bot }: { bot: Bot }) {
  const [loaded, setLoaded] = useState(false);
  /** Null while the stream is connecting; false once it has failed or been cut off. */
  const [live, setLive] = useState<boolean | null>(null);
  const [controller, setCtl] = useState<"bot" | "human">("bot");
  const [url, setUrl] = useState("");
  /** Where the screen actually is — `url` is also what the person is typing. */
  const [pageUrl, setPageUrl] = useState("");
  const [editingUrl, setEditingUrl] = useState(false);
  const [typing, setTyping] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [desktopUrl, setDesktopUrl] = useState<string | null>(null);
  const [desktopSize, setDesktopSize] = useState<string | null>(null);
  const [desktopState, setDesktopState] = useState<DesktopState>("connecting");
  const [display, setDisplay] = useState<"desktop" | "page">("desktop");
  const screen = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState(DEFAULT_SIZE);
  /** Keys held down on the bot's computer, by the physical key that holds them. */
  const keys = useRef(new Map<string, string>());
  const buttons = useRef(new Map<number, { button: MouseButton; clicks: number }>());
  const pointer = useRef({ x: 0, y: 0 });
  const lastDown = useRef({ at: 0, x: 0, y: 0, button: -1, clicks: 0 });
  const human = controller === "human";
  const [teaching, setTeaching] = useState<Teaching | null>(null);
  const [goal, setGoal] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const recording = teaching?.recording ?? null;
  const onDesktop = !!desktopUrl && display === "desktop" && !recording;

  const refreshTeaching = useCallback(async () => {
    try {
      setTeaching(await teachingStatus(bot.id));
    } catch {
      /* the status line just stays as it was */
    }
  }, [bot.id]);

  useEffect(() => {
    void refreshTeaching();
    const t = setInterval(() => void refreshTeaching(), 2000);
    return () => clearInterval(t);
  }, [refreshTeaching]);

  const teach = async () => {
    if (!goal?.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await startTeaching(bot.id, goal.trim());
      setGoal(null);
      setCtl("human");
      screen.current?.focus();
      await refreshTeaching();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const finish = async (cancel: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await stopTeaching(bot.id, cancel);
      setCtl("bot");
      await refreshTeaching();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Status: who holds the screen, where it is.
  const refreshStatus = useCallback(async () => {
    try {
      const status = await computerStatus();
      setReachable(status.reachable);
      setDesktopUrl(status.desktop_url ?? null);
      setDesktopSize(status.desktop_size ?? null);
      const mine = status.screens?.find((s) => s.screen_id === bot.id);
      if (mine) {
        setCtl(mine.controller === "human" ? "human" : "bot");
        setPageUrl(mine.url);
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

  // The picture: one stream for as long as the pane is open, reconnecting after the
  // computer restarts. A frame still being drawn is replaced, never queued behind.
  useEffect(() => {
    // The desktop view shows the whole screen; the page is not streamed under it.
    if (onDesktop) return;
    const abort = new AbortController();
    const surface = canvas.current;
    const ctx = surface?.getContext("2d");
    if (surface) ctx?.clearRect(0, 0, surface.width, surface.height);
    setLoaded(false);
    setLive(null);
    let streaming = false;
    let drawing = false;
    let next: Blob | null = null;

    const draw = async (jpeg: Blob) => {
      next = jpeg;
      if (drawing) return;
      drawing = true;
      try {
        while (next && !abort.signal.aborted) {
          const frame = next;
          next = null;
          const bitmap = await createImageBitmap(frame);
          // A frame is the bot's viewport at one pixel per CSS pixel, so its size is
          // the coordinate space every click is sent in.
          if (surface && (surface.width !== bitmap.width || surface.height !== bitmap.height)) {
            surface.width = bitmap.width;
            surface.height = bitmap.height;
            setSize({ width: bitmap.width, height: bitmap.height });
          }
          ctx?.drawImage(bitmap, 0, 0);
          bitmap.close();
        }
      } catch {
        /* a frame that will not decode is skipped; the next one replaces it */
      } finally {
        drawing = false;
      }
    };

    void (async () => {
      let wait = 500;
      while (!abort.signal.aborted) {
        try {
          await readFrames(streamUrl(bot.id), abort.signal, (jpeg) => {
            if (!streaming) {
              streaming = true;
              wait = 500;
              setLoaded(true);
              setLive(true);
            }
            void draw(jpeg);
          });
        } catch {
          /* unreachable or cut off: retried below */
        }
        if (abort.signal.aborted) return;
        streaming = false;
        setLive(false);
        await sleep(wait, abort.signal);
        wait = Math.min(wait * 2, 5000);
      }
    })();
    return () => abort.abort();
  }, [bot.id, onDesktop]);

  // Opening a bot's computer shows that bot's window on top.
  useEffect(() => {
    if (onDesktop) void showScreen(bot.id).catch(() => undefined);
  }, [bot.id, onDesktop]);

  const editing = useRef(editingUrl);
  useEffect(() => {
    editing.current = editingUrl;
  }, [editingUrl]);
  const queue = useMemo(
    () =>
      inputQueue(
        async (events) => {
          const result = await sendInputs(bot.id, events);
          if (result.url) setPageUrl(result.url);
          if (result.url && !editing.current) setUrl(result.url);
        },
        (cause) => {
          setError(cause.message);
          void refreshStatus();
        },
      ),
    // `refreshStatus` changes as the URL box is edited; the queue must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bot.id],
  );
  const input = (event: HumanInput) => {
    if (error) setError(null);
    queue.push(event);
  };

  /** Let go of everything held, on this side and the computer's. */
  const releaseAll = useCallback(() => {
    for (const key of keys.current.values()) queue.push({ kind: "keyup", key });
    for (const held of buttons.current.values()) {
      queue.push({ kind: "up", ...pointer.current, ...held });
    }
    keys.current.clear();
    buttons.current.clear();
  }, [queue]);

  const toggleControl = async () => {
    setError(null);
    const target = human ? "bot" : "human";
    if (target === "bot") {
      // The computer lets go of anything still held when it gets the screen back.
      queue.clear();
      keys.current.clear();
      buttons.current.clear();
    }
    try {
      await setController(bot.id, target);
      setCtl(target);
      if (target === "human") screen.current?.focus({ preventScroll: true });
    } catch (cause) {
      setError((cause as Error).message);
    }
  };

  /** Where on the bot's screen a point on ours is. */
  const at = (e: { clientX: number; clientY: number }) => {
    const rect = canvas.current?.getBoundingClientRect();
    if (!rect || !rect.width) return pointer.current;
    const { width, height } = canvas.current ?? DEFAULT_SIZE;
    const x = ((e.clientX - rect.left) / rect.width) * width;
    const y = ((e.clientY - rect.top) / rect.height) * height;
    pointer.current = {
      x: Math.round(Math.min(Math.max(x, 0), width - 1)),
      y: Math.round(Math.min(Math.max(y, 0), height - 1)),
    };
    return pointer.current;
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const button = BUTTONS[e.button];
    if (!human || !button) return;
    e.preventDefault();
    screen.current?.focus({ preventScroll: true });
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = at(e);
    const last = lastDown.current;
    const now = performance.now();
    const again =
      last.button === e.button &&
      now - last.at < DOUBLE_CLICK_MS &&
      Math.hypot(p.x - last.x, p.y - last.y) < 6;
    const clicks = again ? Math.min(last.clicks + 1, 3) : 1;
    lastDown.current = { at: now, ...p, button: e.button, clicks };
    buttons.current.set(e.button, { button, clicks });
    input({ kind: "down", ...p, button, clicks });
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const held = buttons.current.get(e.button);
    if (!human || !held) return;
    buttons.current.delete(e.button);
    input({ kind: "up", ...at(e), ...held });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    // A paste is left to the browser, whose paste event brings the person's clipboard.
    if (!human || isPaste(e)) return;
    e.preventDefault();
    const key = remoteKey(e);
    if (!key) return;
    keys.current.set(e.code || key, key);
    input({ kind: "keydown", key });
  };

  const onKeyUp = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!human) return;
    const code = e.code || e.key;
    const key = keys.current.get(code);
    if (key === undefined) return;
    e.preventDefault();
    keys.current.delete(code);
    if (IS_MAC && e.key === "Meta") {
      // macOS sends no key-up for a key let go while ⌘ was down.
      for (const other of keys.current.values()) input({ kind: "keyup", key: other });
      keys.current.clear();
    }
    input({ kind: "keyup", key });
  };

  // Wheel and paste need listeners React does not give: the wheel one must be able to
  // stop the pane itself scrolling, and a paste arrives at the document.
  useEffect(() => {
    const el = screen.current;
    if (!el || !human) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const page = canvas.current?.height ?? DEFAULT_SIZE.height;
      const scale = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? page : 1;
      queue.push({ kind: "wheel", ...at(e), dx: e.deltaX * scale, dy: e.deltaY * scale });
    };
    const onPaste = (e: ClipboardEvent) => {
      if (document.activeElement !== el) return;
      const text = e.clipboardData?.getData("text/plain");
      if (!text) return;
      e.preventDefault();
      queue.push({ kind: "paste", text: text.slice(0, PASTE_MAX) });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    document.addEventListener("paste", onPaste);
    return () => {
      el.removeEventListener("wheel", onWheel);
      document.removeEventListener("paste", onPaste);
    };
    // `at` reads refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [human, queue]);

  return (
    <div>
      <div className="screen-bar">
        {desktopUrl && !recording && (
          <div className="seg" role="tablist" aria-label="View">
            <button
              type="button"
              role="tab"
              aria-selected={display === "desktop"}
              className={cx(display === "desktop" && "on")}
              title="The computer's own screen, with this bot's Chrome window in front"
              onClick={() => setDisplay("desktop")}
            >
              Desktop
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={display === "page"}
              className={cx(display === "page" && "on")}
              title="Only this bot's page"
              onClick={() => setDisplay("page")}
            >
              Page
            </button>
          </div>
        )}
        {!onDesktop && (
          <>
            <button
              type="button"
              className="ibtn"
              disabled={!human}
              onClick={() => input({ kind: "back" })}
              aria-label="Back"
            >
              <ArrowLeftIcon />
            </button>
            <button
              type="button"
              className="ibtn"
              disabled={!human}
              onClick={() => input({ kind: "reload" })}
              aria-label="Reload"
            >
              <ArrowPathIcon />
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
                  input({ kind: "navigate", url: url.trim() });
                  (e.target as HTMLInputElement).blur();
                }
              }}
              aria-label="Address"
              placeholder="about:blank"
            />
          </>
        )}
      </div>

      {onDesktop && desktopUrl ? (
        <div
          className={cx("screen", "desktop", human && "human")}
          style={{ aspectRatio: (desktopSize ?? "1440x900").replace("x", " / ") }}
        >
          <DesktopView
            socket={desktopSocket(desktopUrl)}
            control={human}
            label={`The computer's desktop, with ${bot.name}'s window in front`}
            onState={setDesktopState}
          />
          {desktopState !== "live" && (
            <div className="screen-overlay">
              {desktopState === "lost" ? "Reconnecting to the desktop…" : "Connecting to the desktop…"}
            </div>
          )}
          {desktopState === "live" && (
            <span className="screen-badge">{human ? "You have control" : "Live desktop"}</span>
          )}
        </div>
      ) : (
        <div
          ref={screen}
          className={cx("screen", human && "human")}
          style={{ aspectRatio: `${size.width} / ${size.height}` }}
          tabIndex={human ? 0 : -1}
          onPointerDown={onPointerDown}
          onPointerMove={(e) => {
            if (human) input({ kind: "move", ...at(e) });
          }}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onMouseDown={(e) => {
            // No text selection or focus change on this page: the press is the bot's.
            if (human) e.preventDefault();
          }}
          onContextMenu={(e) => {
            if (human) e.preventDefault();
          }}
          onKeyDown={onKeyDown}
          onKeyUp={onKeyUp}
          onBlur={() => {
            if (human) releaseAll();
          }}
          aria-label={human ? "Bot screen — you have control" : "Bot screen"}
        >
          <canvas
            ref={canvas}
            width={DEFAULT_SIZE.width}
            height={DEFAULT_SIZE.height}
            role="img"
            aria-label={`${bot.name}'s screen`}
          />
          {(reachable === false || (live === false && !loaded)) && (
            <div className="screen-overlay">
              The cloud computer isn&apos;t running.
              <br />
              Start it with <code>scripts/computer.sh up</code>
            </div>
          )}
          {loaded && reachable !== false && (!pageUrl || pageUrl === "about:blank") && (
            // A browser with nothing open is a white page; say so, rather than look broken.
            <div className="screen-overlay screen-empty">
              {human
                ? "Nothing is open yet. Type an address in the bar above, then click and type on the page."
                : `Nothing is open on ${bot.name}'s screen yet. Give it a task, or take control and type an address.`}
            </div>
          )}
          {loaded && (
            <span className="screen-badge">
              {!live ? "Reconnecting…" : human ? "You have control" : "Live"}
            </span>
          )}
        </div>
      )}

      {human ? (
        <>
          <p className="screen-help">
            {onDesktop
              ? `You're at the computer, with ${bot.name}'s Chrome window in front. Use it as your own — its address bar, tabs and menus all work; click into it first, and paste with your usual shortcut. For text from an input method, type it below. Hand control back when you're done.`
              : "Use the screen as you would your own: move, click, drag, scroll and type while it has focus, and paste with your usual shortcut. For text from an input method, type it below. Hand control back when you're done."}
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
                  input({ kind: "type", text: typing });
                  setTyping("");
                }
              }}
            />
            <button
              type="button"
              className="pbtn"
              disabled={!typing}
              onClick={() => {
                input({ kind: "type", text: typing });
                setTyping("");
              }}
            >
              Type
            </button>
            <button
              type="button"
              className="pbtn"
              title="Press Enter"
              aria-label="Press Enter"
              onClick={() => input({ kind: "key", key: "Enter" })}
            >
              <ArrowTurnDownLeftIcon />
            </button>
          </div>
        </>
      ) : (
        <p className="screen-help">
          {onDesktop
            ? `Watching the computer's desktop, with ${bot.name}'s Chrome window in front. `
            : `Watching ${bot.name}'s screen. `}
          Take control to sign in, solve a CAPTCHA or finish a step yourself — the bot pauses
          until you hand it back.
        </p>
      )}

      {recording ? (
        <div className="teach-bar" role="status">
          <span className="rec-dot" />
          <div className="grow">
            <strong>Recording:</strong> {recording.goal}
            <div className="muted">
              {Math.max(0, (teaching?.computer?.steps ?? 1) - 1)} steps ·{" "}
              {elapsed(teaching?.computer?.elapsed ?? 0)}
              {teaching?.computer?.full ? " · limit reached, press Stop" : ""}
            </div>
          </div>
          <button type="button" className="pbtn" disabled={busy} onClick={() => void finish(true)}>
            Cancel
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={busy}
            onClick={() => void finish(false)}
          >
            Stop &amp; write skill
          </button>
        </div>
      ) : goal !== null ? (
        <div className="teach-bar">
          <input
            className="urlbox grow"
            style={{ fontFamily: "var(--sans)" }}
            value={goal}
            autoFocus
            placeholder="What will you show? e.g. Download last month's invoice"
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void teach();
              if (e.key === "Escape") setGoal(null);
            }}
          />
          <button type="button" className="pbtn" onClick={() => setGoal(null)}>
            Cancel
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={busy || !goal.trim()}
            onClick={() => void teach()}
          >
            Start recording
          </button>
        </div>
      ) : null}

      {error && <ErrorNotice>{error}</ErrorNotice>}

      <div className="control-row">
        {!recording && (
          <button
            type="button"
            className={cx("pbtn", !human && "primary")}
            onClick={() => void toggleControl()}
          >
            {human ? "Return control to bot" : "Take control"}
          </button>
        )}
        {!recording && goal === null && (
          <button
            type="button"
            className="pbtn"
            title="Do a task yourself on this screen; the bot learns it as a skill"
            onClick={() => setGoal("")}
          >
            <AcademicCapIcon /> Teach a task
          </button>
        )}
        <button
          type="button"
          className="pbtn"
          onClick={() => {
            if (window.confirm("Restart the cloud computer? Open pages close; sign-ins are kept.")) {
              void resetComputer().catch((cause: Error) => setError(cause.message));
            }
          }}
        >
          Recover computer
        </button>
        {desktopUrl && (
          <a
            className="pbtn"
            href={desktopUrl}
            target="_blank"
            rel="noreferrer"
            title="Every bot's window on the computer's own screen, in a new tab"
          >
            Whole desktop ↗
          </a>
        )}
      </div>
    </div>
  );
}

function elapsed(seconds: number): string {
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

