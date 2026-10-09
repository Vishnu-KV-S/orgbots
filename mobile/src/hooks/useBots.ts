import { useFocusEffect } from "expo-router";
import { useCallback, useRef, useState } from "react";
import { AppState } from "react-native";
import { type Bot, listBots } from "@/lib/api";

const POLL_MS = 4000;

/**
 * The bots list, refreshed while the screen is in front and the app is active, so
 * "working", unread dots and "needs you" stay live without pull-to-refresh.
 */
export function useBots() {
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const { bots } = await listBots();
      setBots(bots);
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }, [load]);

  useFocusEffect(
    useCallback(() => {
      void load();
      const start = () => {
        if (timer.current) clearInterval(timer.current);
        timer.current = setInterval(() => void load(), POLL_MS);
      };
      start();
      const sub = AppState.addEventListener("change", (state) => {
        if (state === "active") {
          void load();
          start();
        } else if (timer.current) {
          clearInterval(timer.current);
          timer.current = null;
        }
      });
      return () => {
        sub.remove();
        if (timer.current) clearInterval(timer.current);
        timer.current = null;
      };
    }, [load]),
  );

  return { bots, error, refreshing, refresh, reload: load, setBots };
}
