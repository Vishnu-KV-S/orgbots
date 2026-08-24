import type { EventRow } from "@/lib/types";

/** Topics we colour. Everything else is neutral — an unknown topic is not an error. */
const FAILURE_SUFFIXES = ["failed", "rejected"];
const SUCCESS_SUFFIXES = ["succeeded", "accepted"];

export type EventTone = "ok" | "failed" | "";

export function toneOf(topic: string): EventTone {
  if (FAILURE_SUFFIXES.some((suffix) => topic.endsWith(suffix))) return "failed";
  if (SUCCESS_SUFFIXES.some((suffix) => topic.endsWith(suffix))) return "ok";
  return "";
}

/**
 * The most useful field in a payload, in the order we would ask for it. Events
 * carry arbitrary payloads, and a feed line has room for one fact.
 */
const INTERESTING = ["reason", "status", "outcome", "title", "error", "detail", "trigger"];

export function summarize(event: EventRow): string {
  const payload = event.payload ?? {};
  for (const key of INTERESTING) {
    const value = payload[key];
    if (typeof value === "string" && value) return `${key}=${value}`;
  }
  return event.run_id ? `run ${event.run_id.slice(0, 8)}` : "";
}
