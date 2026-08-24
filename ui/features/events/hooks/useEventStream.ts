"use client";

import { useEffect, useState } from "react";
import { streamUrl } from "@/lib/api";
import type { EventRow } from "@/lib/types";

/**
 * The org-wide SSE tail.
 *
 * It waits for the activity poll to hand it a seed, then opens the stream at
 * the highest id in that seed — otherwise a viewer that has just loaded the
 * page watches the org's whole history scroll past before reaching the present.
 *
 * The server sends every topic on the default SSE channel, so `onmessage`
 * catches topics this build has never heard of.
 */

const KEEP = 300;

export interface EventStream {
  events: EventRow[];
  connected: boolean;
}

export function useEventStream(orgId: string, seed: EventRow[], seedReady: boolean): EventStream {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [after, setAfter] = useState<number | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    setEvents([]);
    setAfter(null);
  }, [orgId]);

  useEffect(() => {
    if (!seedReady || after !== null) return;
    setEvents([...seed].reverse()); // the activity poll returns newest first
    setAfter(seed.length ? Math.max(...seed.map((event) => event.id)) : 0);
  }, [seedReady, seed, after]);

  useEffect(() => {
    if (after === null) return;
    const source = new EventSource(streamUrl(orgId, after));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (raw: MessageEvent<string>) => {
      try {
        const event = JSON.parse(raw.data) as EventRow;
        setEvents((current) =>
          current.some((seen) => seen.id === event.id)
            ? current
            : [...current, event].slice(-KEEP),
        );
      } catch {
        /* a malformed frame is not worth tearing the feed down over */
      }
    };
    return () => source.close();
    // `after` is the opening cursor only. Re-running on every arriving event
    // would reopen the stream continuously, so it is deliberately read once.
  }, [orgId, after]);

  return { events, connected };
}
