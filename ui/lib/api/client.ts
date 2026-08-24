/**
 * The transport. One place that knows how a request is made and how a failure
 * is turned into an Error, so every endpoint in `observe.ts` is a one-liner and
 * a change of base URL, header or error shape is a change to this file alone.
 */

/** Everything goes through the same-origin proxy in `app/rt`. */
export const OBSERVE_BASE = "/rt/v1/observe";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${OBSERVE_BASE}${path}`, { signal, cache: "no-store" });
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? body;
    } catch {
      /* a non-JSON error body is still the most useful thing we have */
    }
    throw new ApiError(detail || response.statusText, response.status);
  }
  return (await response.json()) as T;
}

/** `?a=1&b=2` from the entries that actually have a value. */
export function query(params: Record<string, string | number | undefined>): string {
  const pairs = Object.entries(params).filter(([, value]) => value !== undefined);
  if (pairs.length === 0) return "";
  return `?${pairs.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")}`;
}
