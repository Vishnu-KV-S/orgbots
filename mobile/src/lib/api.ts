/**
 * The Orgbots API, as the phone reaches it.
 *
 * The app talks to the same web server people open in a browser, through its
 * same-origin proxy (`ui/app/rt/[...path]/route.ts`): `https://your-server/rt/v1/...`.
 * So the API stays a private service behind the web app and the phone needs exactly
 * one address. With members, the session is the web app's own `aor_session` cookie,
 * set when the person signs in on the server's `/signin` page inside the app; the
 * native networking stack keeps it and sends it with every request here.
 *
 * Types are the subset of `ui/lib/api/bots.ts` the app uses, kept field-for-field.
 */

let server = "";
let onUnauthorized: (() => void) | null = null;

export function setServer(url: string) {
  server = url.replace(/\/+$/, "");
}

export function getServer() {
  return server;
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

/** `https://host/rt/v1/<path>` — the proxied API path for anything under `/v1`. */
export const apiUrl = (path: string) => `${server}/rt/v1${path}`;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn a server URL as typed into one we can call: scheme added, path dropped. */
export function normalizeServer(input: string): string {
  let value = input.trim();
  if (!value) return "";
  if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
  try {
    const url = new URL(value);
    return `${url.protocol}//${url.host}`;
  } catch {
    return "";
  }
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  if (!server) throw new ApiError("No server is set", 0);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  init.signal?.addEventListener("abort", () => controller.abort());
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      credentials: "include",
      ...init,
      headers: { accept: "application/json", ...(init.headers ?? {}) },
      signal: controller.signal,
    });
  } catch {
    throw new ApiError(
      controller.signal.aborted && !init.signal?.aborted
        ? `${server} took too long to answer`
        : `Can't reach ${server}`,
      0,
    );
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401 && !path.startsWith("/auth/")) onUnauthorized?.();
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? body;
    } catch {
      /* a non-JSON error body is still the most useful thing we have */
    }
    throw new ApiError(detail || `HTTP ${response.status}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

// --- types ---------------------------------------------------------------------------

export interface Appearance {
  shape: "orb" | "cube" | "capsule" | "pod" | "tv";
  body: string;
  glow: string;
  eyes: "pill" | "round" | "square" | "visor" | "dot";
  top: "ring" | "knobs" | "antenna" | "ears" | "halo" | "none";
  finish: "gloss" | "matte" | "metal" | "pearl";
}

export interface BotBrief {
  mission: string;
  duties: string[];
  boundaries: string[];
  style: string;
  escalation: string;
  notes: string;
}

export const EMPTY_BRIEF: BotBrief = {
  mission: "",
  duties: [],
  boundaries: [],
  style: "",
  escalation: "",
  notes: "",
};

export interface Bot {
  id: string;
  name: string;
  label: string;
  description: string;
  avatar: string;
  brief: Partial<BotBrief>;
  pinned: boolean;
  hidden: boolean;
  unread: boolean;
  needs_attention: boolean;
  working: boolean;
  run_status: string | null;
  parent_bot_id: string | null;
  created_by: "person" | "bot";
  appearance: Partial<Appearance>;
  owner_member_id: string | null;
  visibility: "private" | "team";
  created_at: string;
  updated_at: string;
  last_message: BotMessage | null;
}

export interface BotAction {
  type: string;
  element?: number;
  url?: string;
  text?: string;
  key?: string;
  option?: string;
  direction?: string;
  seconds?: number;
  submit?: boolean;
  secret?: boolean;
  page_url?: string;
  host?: string;
  element_label?: string;
  bot?: string;
  label?: string;
  path?: string;
  to?: string;
  paths?: string[];
  name?: string;
  verdict?: "allow" | "ask" | "deny";
}

export type CredentialKind =
  | "email"
  | "username"
  | "phone"
  | "password"
  | "new_password"
  | "confirm_password"
  | "otp"
  | "name"
  | "text";

export interface CredentialField {
  key: string;
  kind: CredentialKind;
  label: string;
}

export interface Attachment {
  id: string;
  path: string;
  name: string;
  media_type: string;
  bytes: number;
  kind: string;
}

export type MessageRole =
  | "user"
  | "bot"
  | "activity"
  | "approval"
  | "system"
  | "error"
  | "credentials";

export interface BotMessage {
  id: string;
  seq: number;
  bot_id: string;
  role: MessageRole;
  content: string;
  payload: {
    action?: BotAction;
    ok?: boolean | null;
    error?: string | null;
    url?: string | null;
    title?: string | null;
    note?: string;
    pending_id?: string;
    reason?: string;
    decision?: string;
    from?: { member_id: string; name: string };
    credential_request_id?: string;
    host?: string;
    page_url?: string;
    purpose?: "sign_in" | "sign_up" | "verify";
    fields?: CredentialField[];
    saved?: { id: string; label: string }[] | boolean;
    retry?: boolean;
    screenshot_id?: string;
    attachments?: Attachment[];
    routine?: string;
    output?: string;
    exit_code?: number | null;
  };
  run_id: string | null;
  reply_to: string | null;
  created_at: string;
  reactions?: string[];
}

export interface MessagePage {
  messages: BotMessage[];
  pending: string[];
  credential_requests: string[];
  working: boolean;
  run_status: string | null;
}

export interface Sent {
  run_id: string | null;
  admitted: boolean;
  refusal_reason: string | null;
}

export type Role = "owner" | "admin" | "member";

export interface Me {
  id: string;
  email: string;
  name: string;
  role: Role;
  organization_id: string;
}

export interface BotDraft {
  name: string;
  label: string;
  description: string;
  avatar: string;
  brief: BotBrief;
  appearance?: Appearance;
}

export interface TeamFile {
  id: string;
  path: string;
  name: string;
  media_type: string;
  bytes: number;
  kind: string;
}

export type HumanInput =
  | { kind: "down" | "up"; x: number; y: number; button: "left"; clicks: number }
  | { kind: "move"; x: number; y: number }
  | { kind: "wheel"; x: number; y: number; dx: number; dy: number }
  | { kind: "type"; text: string }
  | { kind: "key"; key: string }
  | { kind: "navigate"; url: string }
  | { kind: "back" | "forward" | "reload" };

// --- endpoints -----------------------------------------------------------------------

/** Is this an Orgbots server, and who am I on it. */
export const authMe = (timeoutMs = 10_000) =>
  request<{ mode: "none" | "members"; member: Me | null }>("/auth/me", {}, timeoutMs);

export const signOut = () => request<{ signed_out: boolean }>("/auth/sign-out", json("POST"));

export const listBots = () => request<{ organization_id: string; bots: Bot[] }>("/bots");

export const createBot = (draft: BotDraft) => request<Bot>("/bots", json("POST", draft));

export const deleteBot = (id: string) =>
  request<{ deleted: string[] }>(`/bots/${id}?with_helpers=false`, { method: "DELETE" });

export const updateBot = (id: string, fields: Partial<Pick<Bot, "pinned" | "hidden" | "name">>) =>
  request<Bot>(`/bots/${id}`, json("PATCH", fields));

export const markRead = (id: string) =>
  request<unknown>(`/bots/${id}/read`, json("POST", { unread: false }));

export const stopBot = (id: string) => request<unknown>(`/bots/${id}/stop`, json("POST"));

export const fetchMessages = (id: string, after: number, signal?: AbortSignal) =>
  request<MessagePage>(`/bots/${id}/messages?after=${after}`, { signal });

export const sendMessage = (id: string, text: string, attachments: string[] = []) =>
  request<Sent>(
    `/bots/${id}/messages`,
    json("POST", { text, reply_to: null, attachments, voice: false }),
  );

export const decide = (id: string, pendingId: string, decision: "once" | "always" | "deny") =>
  request<Sent>(`/bots/${id}/pending/${pendingId}`, json("POST", { decision }));

/** The values go in this one request body and nowhere else. */
export const submitCredentials = (
  id: string,
  requestId: string,
  values: Record<string, string>,
  save: boolean,
) => request<Sent>(`/bots/${id}/credentials/${requestId}`, json("POST", { values, save }));

export const chooseSavedLogin = (id: string, requestId: string, entryId: string) =>
  request<Sent>(`/bots/${id}/credentials/${requestId}`, json("POST", { use_entry_id: entryId }));

export const cancelCredentials = (id: string, requestId: string) =>
  request<Sent>(`/bots/${id}/credentials/${requestId}/cancel`, json("POST"));

export const reactToMessage = (botId: string, messageId: string, emoji: string, on: boolean) =>
  request<{ message_id: string; reactions: string[] }>(
    `/bots/${botId}/messages/${messageId}/reactions`,
    json("POST", { emoji, on }),
  );

/** Upload into the team's drive; a message then carries the returned ids. */
export const uploadFiles = (
  id: string,
  files: { name: string; media_type: string; data: string }[],
) => request<{ files: TeamFile[] }>(`/bots/${id}/files/upload`, json("POST", { files }), 120_000);

export const setController = (id: string, controller: "bot" | "human") =>
  request<unknown>(`/bots/${id}/computer/control`, json("POST", { controller }));

export const sendInputs = (id: string, events: HumanInput[]) =>
  request<{ ok: boolean; url: string }>(`/bots/${id}/computer/inputs`, json("POST", { events }));

export const screenshotUrl = (botId: string, screenshotId: string) =>
  apiUrl(`/bots/${botId}/screenshots/${screenshotId}`);

export const streamUrl = (botId: string) => apiUrl(`/bots/${botId}/computer/stream`);

/** The bot's screen is a 1280×800 browser viewport; input is in its pixels. */
export const VIEWPORT = { width: 1280, height: 800 };

export const getBot = (id: string) => request<Bot>(`/bots/${id}`);
