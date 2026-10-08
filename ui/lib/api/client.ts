/**
 * The transport. One place that knows how a request is made and how a failure
 * is turned into an Error, so every endpoint in `observe.ts` and `control.ts` is
 * a one-liner and a change of base URL, header or error shape is a change to
 * this file alone.
 */

/** Everything goes through the same-origin proxy in `app/rt`. */
export const OBSERVE_BASE = "/rt/v1/observe";
export const CONTROL_BASE = "/rt/v1/control";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 409. The world moved: a stale plan, a plan already applied, a file that
   * changed under an editor, a kill switch already engaged. The one class of
   * failure a caller should offer to resolve rather than merely report. */
  get isConflict() {
    return this.status === 409;
  }

  /** 422. The documents are wrong — a save that will not parse, a corpus that
   * does not cohere. The message is the thing to show; it names the file. */
  get isInvalid() {
    return this.status === 422;
  }
}

/**
 * Turn a non-OK response into an `ApiError` carrying the runtime's own message.
 *
 * Extracted from `get()` rather than duplicated into each verb: the write verbs
 * need it more than the read one does, because 409 and 422 are the statuses a
 * writer has to act on, and a second copy would be a second answer to what a
 * failure body looks like.
 */
async function toError(response: Response): Promise<ApiError> {
  const body = await response.text();
  let detail = body;
  try {
    detail = (JSON.parse(body) as { detail?: string }).detail ?? body;
  } catch {
    /* a non-JSON error body is still the most useful thing we have */
  }
  return new ApiError(detail || response.statusText, response.status);
}

export async function request<T>(url: string, init: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(`${OBSERVE_BASE}${path}`, { signal });
}

/** GET against the control surface — the listings and the file reads. */
export async function getControl<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(`${CONTROL_BASE}${path}`, { signal });
}

function withBody<T>(method: string) {
  return (path: string, body?: unknown, signal?: AbortSignal): Promise<T> =>
    request<T>(`${CONTROL_BASE}${path}`, {
      method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal,
    });
}

/** The write verbs. Control surface only — `app/rt` refuses them anywhere else. */
export const post = <T,>(path: string, body?: unknown, signal?: AbortSignal) =>
  withBody<T>("POST")(path, body, signal);
export const put = <T,>(path: string, body?: unknown, signal?: AbortSignal) =>
  withBody<T>("PUT")(path, body, signal);
export const del = <T,>(path: string, signal?: AbortSignal): Promise<T> =>
  request<T>(`${CONTROL_BASE}${path}`, { method: "DELETE", signal });

/** `?a=1&b=2` from the entries that actually have a value. */
export function query(params: Record<string, string | number | undefined>): string {
  const pairs = Object.entries(params).filter(([, value]) => value !== undefined);
  if (pairs.length === 0) return "";
  return `?${pairs.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")}`;
}
