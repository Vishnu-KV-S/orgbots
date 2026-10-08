"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { type BotMessage, fetchMessages } from "@/lib/api/bots";

const FAST_MS = 1000;
const SLOW_MS = 3000;

export interface Conversation {
  messages: BotMessage[];
  /** Pending-action ids still waiting on a decision. */
  pending: Set<string>;
  working: boolean;
  runStatus: string | null;
  error: string | null;
  loaded: boolean;
  /** Fetch now, outside the timer — after sending, so the message appears at once. */
  poke: () => void;
}

/**
 * The transcript of one bot, kept up to date by polling with a cursor.
 *
 * Append-only on the server (`seq` only grows), so each poll asks for what is after
 * the last row it has and appends. Fast while the bot is working — the person is
 * watching each step land — and slow when it is idle. Switching bots resets it.
 */
export function useConversation(botId: string | null): Conversation {
  const [messages, setMessages] = useState<BotMessage[]>([]);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [working, setWorking] = useState(false);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const cursor = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflight = useRef(false);
  const current = useRef(botId);

  const tick = useCallback(async () => {
    const id = current.current;
    if (!id || inflight.current) return;
    inflight.current = true;
    let next = SLOW_MS;
    try {
      const page = await fetchMessages(id, cursor.current);
      if (current.current !== id) return;
      if (page.messages.length > 0) {
        cursor.current = page.messages[page.messages.length - 1].seq;
        setMessages((prev) => {
          const seen = new Set(prev.map((m) => m.id));
          return [...prev, ...page.messages.filter((m) => !seen.has(m.id))];
        });
      }
      setPending(new Set(page.pending));
      setWorking(page.working);
      setRunStatus(page.run_status);
      setError(null);
      setLoaded(true);
      next = page.working ? FAST_MS : SLOW_MS;
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      inflight.current = false;
      if (current.current === id) {
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void tick(), next);
      }
    }
  }, []);

  useEffect(() => {
    current.current = botId;
    cursor.current = 0;
    setMessages([]);
    setPending(new Set());
    setWorking(false);
    setRunStatus(null);
    setError(null);
    setLoaded(false);
    if (timer.current) clearTimeout(timer.current);
    if (botId) void tick();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [botId, tick]);

  const poke = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    void tick();
  }, [tick]);

  return { messages, pending, working, runStatus, error, loaded, poke };
}
