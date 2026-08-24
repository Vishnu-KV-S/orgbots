"use client";

import { useCallback, useState } from "react";
import { ApiError } from "@/lib/api";

/**
 * One write, and the four states it can be in.
 *
 * `useResource` owns the rules for reading; this owns the rules for writing, and
 * they are not the same rules. A failed poll keeps the last good data on screen
 * because the alternative is a canvas that blanks itself; a failed *action* must
 * be loud, must stay on screen until it is dismissed or superseded, and must
 * never be retried automatically. A write the user did not ask for twice is a
 * second department stopped, or a second run started.
 *
 * `status` is the status code when the failure came from the runtime, so a caller
 * can treat a 409 — the world moved — as something to offer a resolution for
 * rather than as a generic red box.
 */

export interface Action<A extends unknown[], T> {
  run: (...args: A) => Promise<T | null>;
  /** In flight. Disable the control; do not hide it — a button that vanishes
   * mid-click leaves nowhere to put the error. */
  pending: boolean;
  error: string | null;
  /** HTTP status of the last failure, or `null`. 409 is the interesting one. */
  status: number | null;
  result: T | null;
  reset: () => void;
}

export function useAction<A extends unknown[], T>(
  fn: (...args: A) => Promise<T>,
  { onDone }: { onDone?: (result: T) => void } = {},
): Action<A, T> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [result, setResult] = useState<T | null>(null);

  const run = useCallback(
    async (...args: A): Promise<T | null> => {
      setPending(true);
      setError(null);
      setStatus(null);
      try {
        const value = await fn(...args);
        setResult(value);
        onDone?.(value);
        return value;
      } catch (cause) {
        setError((cause as Error).message);
        setStatus(cause instanceof ApiError ? cause.status : null);
        return null;
      } finally {
        // In `finally`, so a component that unmounts mid-flight does not leave a
        // spinner behind for the next thing rendered in its place.
        setPending(false);
      }
    },
    [fn, onDone],
  );

  const reset = useCallback(() => {
    setError(null);
    setStatus(null);
    setResult(null);
  }, []);

  return { run, pending, error, status, result, reset };
}
