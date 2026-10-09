import { useCallback, useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { type BotMessage, fetchMessages } from "@/lib/api";

const FAST_MS = 1000;
const SLOW_MS = 3000;

export interface Conversation {
  messages: BotMessage[];
  /** Pending-action ids still waiting on a decision. */
  pending: Set<string>;
  /** Credential cards still waiting for the person. */
  asking: Set<string>;
  working: boolean;
  error: string | null;
  loaded: boolean;
  /** Fetch now, outside the timer — after sending, so the message appears at once. */
  poke: () => void;
}

interface State {
  messages: BotMessage[];
  pending: Set<string>;
  asking: Set<string>;
  working: boolean;
  error: string | null;
  loaded: boolean;
}

const EMPTY: State = {
  messages: [],
  pending: new Set(),
  asking: new Set(),
  working: false,
  error: null,
  loaded: false,
};

/**
 * One bot's transcript, kept current by polling with a cursor — the same contract as
 * the web app's hook: `seq` only grows, so each poll asks for what follows the last
 * row it has. Fast while the bot works, slow when idle, and paused entirely while the
 * app is in the background so a pocketed phone does not keep the radio awake.
 *
 * The poller lives inside one effect per bot; `poke` reaches it through a ref.
 */
export function useConversation(botId: string): Conversation {
  const [state, setState] = useState<State>(EMPTY);
  const pokeRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    let alive = true;
    let active = AppState.currentState === "active";
    let inflight = false;
    let cursor = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = (ms: number) => {
      if (timer) clearTimeout(timer);
      timer = alive && active ? setTimeout(() => void tick(), ms) : null;
    };

    const tick = async () => {
      if (inflight || !active || !alive) return;
      inflight = true;
      let next = SLOW_MS;
      try {
        const page = await fetchMessages(botId, cursor);
        if (!alive) return;
        if (page.messages.length > 0) cursor = page.messages[page.messages.length - 1].seq;
        setState((prev) => {
          const seen = new Set(prev.messages.map((m) => m.id));
          const fresh = page.messages.filter((m) => !seen.has(m.id));
          return {
            messages: fresh.length ? [...prev.messages, ...fresh] : prev.messages,
            pending: new Set(page.pending),
            asking: new Set(page.credential_requests ?? []),
            working: page.working,
            error: null,
            loaded: true,
          };
        });
        next = page.working ? FAST_MS : SLOW_MS;
      } catch (cause) {
        if (alive) setState((prev) => ({ ...prev, error: (cause as Error).message }));
      } finally {
        inflight = false;
        schedule(next);
      }
    };

    void tick();
    pokeRef.current = () => {
      if (timer) clearTimeout(timer);
      void tick();
    };
    const sub = AppState.addEventListener("change", (appState) => {
      const now = appState === "active";
      if (now === active) return;
      active = now;
      if (timer) clearTimeout(timer);
      if (now) void tick();
    });
    return () => {
      alive = false;
      sub.remove();
      if (timer) clearTimeout(timer);
      pokeRef.current = () => undefined;
    };
  }, [botId]);

  const poke = useCallback(() => pokeRef.current(), []);
  return { ...state, poke };
}
