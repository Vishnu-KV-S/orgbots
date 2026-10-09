"use client";

import type RFB from "@novnc/novnc";
import { useEffect, useRef } from "react";

const XK_CONTROL_L = 0xffe3;
const XK_V = 0x0076;

export type DesktopState = "connecting" | "live" | "lost";

/**
 * The computer's whole desktop, live: the screen a person would sit in front of, with
 * every bot's Chrome window on it — the browser's own tabs, address bar and menus.
 *
 * It is a VNC view (noVNC's client, over the websocket beside the computer's noVNC
 * page). Watching is view-only. While the person holds the screen (`control`) their
 * mouse and keyboard go to the desktop the way a mouse and keyboard plugged into it
 * would, and their clipboard comes with them: a paste shortcut first puts what they
 * copied on the desktop's clipboard, and what they copy there lands on theirs. A ⌘V on
 * a Mac is a Ctrl+V on the computer, which is Linux. It reconnects after the computer
 * restarts.
 */
export function DesktopView({
  socket,
  control,
  label,
  onState,
}: {
  socket: string;
  control: boolean;
  label: string;
  onState?: (state: DesktopState) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const controlling = useRef(control);
  const report = useRef(onState);

  useEffect(() => {
    report.current = onState;
  }, [onState]);

  useEffect(() => {
    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let wait = 500;
    const say = (state: DesktopState) => report.current?.(state);

    const connect = async () => {
      const { default: Client } = await import("@novnc/novnc");
      if (stopped || !host.current) return;
      const client = new Client(host.current, socket, { shared: true });
      client.scaleViewport = true;
      client.resizeSession = false;
      client.focusOnClick = true;
      client.background = "transparent";
      client.viewOnly = !controlling.current;
      client.addEventListener("connect", () => {
        wait = 500;
        say("live");
      });
      client.addEventListener("disconnect", () => {
        if (rfb.current === client) rfb.current = null;
        if (stopped) return;
        say("lost");
        retry = setTimeout(() => void connect(), wait);
        wait = Math.min(wait * 2, 5000);
      });
      client.addEventListener("clipboard", (e) => {
        const text = (e as CustomEvent<{ text: string }>).detail?.text;
        if (text) void navigator.clipboard?.writeText(text).catch(() => undefined);
      });
      rfb.current = client;
    };

    say("connecting");
    void connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      rfb.current?.disconnect();
      rfb.current = null;
    };
  }, [socket]);

  useEffect(() => {
    controlling.current = control;
    const client = rfb.current;
    if (!client) return;
    client.viewOnly = !control;
    if (control) client.focus();
  }, [control]);

  // The paste shortcut, before noVNC sends it on: the desktop's clipboard gets the
  // person's text first, so the computer pastes what they copied.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let swallowUp = false;
    const onKeyDown = (e: KeyboardEvent) => {
      const client = rfb.current;
      if (!client || !controlling.current) return;
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "v") return;
      e.preventDefault();
      e.stopPropagation();
      swallowUp = true;
      const held = e.ctrlKey;
      void (async () => {
        const text = await navigator.clipboard?.readText().catch(() => "");
        if (text) client.clipboardPasteFrom(text);
        if (!held) client.sendKey(XK_CONTROL_L, "ControlLeft", true);
        client.sendKey(XK_V, "KeyV", true);
        client.sendKey(XK_V, "KeyV", false);
        if (!held) client.sendKey(XK_CONTROL_L, "ControlLeft", false);
      })();
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (swallowUp && e.key.toLowerCase() === "v") {
        swallowUp = false;
        e.preventDefault();
        e.stopPropagation();
      }
    };
    el.addEventListener("keydown", onKeyDown, true);
    el.addEventListener("keyup", onKeyUp, true);
    return () => {
      el.removeEventListener("keydown", onKeyDown, true);
      el.removeEventListener("keyup", onKeyUp, true);
    };
  }, []);

  return <div ref={host} className="desktop-view" role="img" aria-label={label} />;
}
