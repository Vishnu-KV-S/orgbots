"use client";

import { useCallback, useState } from "react";

/**
 * A named set of on/off flags. Screens grow toggles the way this one has —
 * plan, delegation, feed, live — and one `useState` each means four setters to
 * thread through a toolbar. Here a toolbar takes the record and one callback,
 * and a new toggle is a new key.
 */
export function useToggles<K extends string>(
  initial: Record<K, boolean>,
): [Record<K, boolean>, (key: K) => void] {
  const [values, setValues] = useState<Record<K, boolean>>(initial);
  const toggle = useCallback(
    (key: K) => setValues((current) => ({ ...current, [key]: !current[key] })),
    [],
  );
  return [values, toggle];
}
