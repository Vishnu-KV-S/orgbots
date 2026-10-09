import type { HumanInput } from "@/lib/api/bots";

/**
 * The two halves of using a bot's screen from here: reading its live picture, and
 * sending a person's input to it in the order, and at the pace, it was made.
 */

const HEADER_END = new Uint8Array([13, 10, 13, 10]);

function indexOf(haystack: Uint8Array, needle: Uint8Array): number {
  outer: for (let i = 0; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

/**
 * Read the computer's MJPEG stream and hand over each JPEG as it arrives. Every part
 * says its length, so parts are cut by that rather than by scanning for the boundary.
 * Resolves when the stream ends (the computer restarted, the screen closed); throws
 * if it cannot be read, or when `signal` aborts.
 */
export async function readFrames(
  url: string,
  signal: AbortSignal,
  onFrame: (jpeg: Blob) => void,
): Promise<void> {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (!response.ok || !response.body) {
    throw new Error(`the live view is not available (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = new Uint8Array(0);
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    const joined = new Uint8Array(buffer.length + value.length);
    joined.set(buffer);
    joined.set(value, buffer.length);
    buffer = joined;
    for (;;) {
      const headerEnd = indexOf(buffer, HEADER_END);
      if (headerEnd < 0) break;
      const length = /content-length:\s*(\d+)/i.exec(decoder.decode(buffer.subarray(0, headerEnd)));
      if (!length) throw new Error("the live view sent a frame without a length");
      const start = headerEnd + HEADER_END.length;
      const end = start + Number(length[1]);
      if (buffer.length < end) break;
      onFrame(new Blob([buffer.slice(start, end)], { type: "image/jpeg" }));
      buffer = buffer.slice(end);
    }
  }
}

/** Most events in one request; the computer takes up to 500. */
const BATCH = 400;
/** Past this, a backlog of pointer motion is dropped (presses and keys never are). */
const BACKLOG = 600;

/**
 * Input on its way to the computer. One request is in flight at a time and the next
 * carries everything made meanwhile, so nothing overtakes anything — a release never
 * lands before its press — and each event keeps the moment it was made at.
 */
export function inputQueue(
  send: (events: HumanInput[]) => Promise<unknown>,
  failed: (cause: Error) => void,
) {
  let pending: HumanInput[] = [];
  let sending = false;

  const flush = async () => {
    if (sending || pending.length === 0) return;
    sending = true;
    const batch = pending.splice(0, BATCH);
    try {
      await send(batch);
    } catch (cause) {
      // What was queued behind a refusal was made for a screen that is not ours.
      pending = [];
      failed(cause as Error);
    } finally {
      sending = false;
    }
    void flush();
  };

  return {
    push(event: HumanInput) {
      pending.push({ ...event, t: Math.round(performance.now() * 10) / 10 });
      if (pending.length > BACKLOG) {
        let last = -1;
        pending.forEach((e, i) => {
          if (e.kind === "move") last = i;
        });
        pending = pending.filter((e, i) => e.kind !== "move" || i === last);
      }
      void flush();
    },
    clear() {
      pending = [];
    },
  };
}

export type InputQueue = ReturnType<typeof inputQueue>;
