"use client";

import { useEffect, useRef } from "react";
import { Pill } from "@/components/ui";
import { cx } from "@/lib/cx";
import { clock } from "@/lib/format";
import type { EventRow } from "@/lib/types";
import { useEventStream } from "./hooks/useEventStream";
import { summarize, toneOf } from "./lib/summarize";

/** The tail pinned along the bottom of the org screen. */
export function EventFeed({
  orgId,
  seed,
  seedReady,
}: {
  orgId: string;
  seed: EventRow[];
  seedReady: boolean;
}) {
  const { events, connected } = useEventStream(orgId, seed, seedReady);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const body = bodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [events.length]);

  return (
    <div className="feed">
      <div className="feed-head">
        <span>event tail</span>
        <Pill status={connected ? "running" : "idle"}>{connected ? "live" : "connecting"}</Pill>
        <span className="spacer" />
        <span>{events.length} shown</span>
      </div>
      <div className="feed-body" ref={bodyRef}>
        {events.length === 0 && <div className="empty">No events yet.</div>}
        {events.map((event) => (
          <EventLine key={event.id} event={event} />
        ))}
      </div>
    </div>
  );
}

function EventLine({ event }: { event: EventRow }) {
  return (
    <div className={cx("feed-line", toneOf(event.topic))}>
      <span className="t">{clock(event.created_at)}</span>
      <span className="topic">{event.topic}</span>
      <span className="who">{event.actor ?? "—"}</span>
      <span className="rest">{summarize(event)}</span>
    </div>
  );
}
