"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Load-once and load-on-a-timer, in one place.
 *
 * Every screen in this viewer does the same three things — fetch, abort on
 * unmount, and say something useful when the runtime is not there — and doing
 * that by hand in each component is how two screens end up disagreeing about
 * what a failed poll should show. The rules are here instead:
 *
 * - an abort is never an error, so navigating away does not flash a red banner;
 * - a failed poll keeps the last good data on screen, because a canvas that
 *   empties itself every time the API blips is worse than a slightly stale one;
 * - `fetcher` is a dependency. Wrap it in `useCallback` or every render restarts
 *   the request.
 */

export interface Resource<T> {
  data: T | null;
  error: string | null;
  /** No data and no error yet — the only honest "still loading" state. */
  loading: boolean;
  /** Force a fetch now, outside the interval. */
  refresh: () => void;
}

export interface PollOptions {
  /** Milliseconds between polls. `0` loads once and stops. */
  intervalMs?: number;
  /** Pause polling without discarding what is on screen. */
  enabled?: boolean;
}

export function useResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { intervalMs = 0, enabled = true }: PollOptions = {},
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const next = await fetcher(signal);
        if (signal.aborted) return;
        setData(next);
        setError(null);
      } catch (cause) {
        if (signal.aborted || (cause as Error).name === "AbortError") return;
        setError((cause as Error).message);
      }
    },
    [fetcher],
  );

  // The first load, and any load forced by `refresh`.
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, nonce]);

  // The timer. Separate from the load above so pausing does not re-fetch.
  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;
    const controller = new AbortController();
    const timer = setInterval(() => void load(controller.signal), intervalMs);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [enabled, intervalMs, load]);

  return {
    data,
    error,
    loading: data === null && error === null,
    refresh: useCallback(() => setNonce((n) => n + 1), []),
  };
}
