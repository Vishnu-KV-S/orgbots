import { request } from "./client";

/**
 * One function per `/v1/bots` and `/v1/computer` endpoint. Same rule as the two
 * sibling files: components never build a URL.
 */

export const BOTS_BASE = "/rt/v1/bots";
export const COMPUTER_BASE = "/rt/v1/computer";

export interface Bot {
  id: string;
  actor_name: string;
  name: string;
  label: string;
  description: string;
  instructions: string;
  avatar: string;
  memory: string;
  pinned: boolean;
  hidden: boolean;
  unread: boolean;
  needs_attention: boolean;
  working: boolean;
  run_status: string | null;
  last_run_id: string | null;
  duplicated_from: string | null;
  parent_bot_id: string | null;
  created_by: "person" | "bot";
  created_at: string;
  updated_at: string;
  last_message: BotMessage | null;
}

export type MessageRole = "user" | "bot" | "activity" | "approval" | "system" | "error";

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
  /** create_bot / ask_bot / bot_answer: the helper's name. */
  bot?: string;
  label?: string;
}

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
    kind?: string;
    pending_id?: string;
    reason?: string;
    decision?: string;
    controller?: string;
    helper_id?: string;
    status?: string;
    from_bot_id?: string;
    from_bot_name?: string;
  };
  run_id: string | null;
  reply_to: string | null;
  created_at: string;
}

export interface MessagePage {
  messages: BotMessage[];
  pending: string[];
  working: boolean;
  run_status: string | null;
}

export interface BotRule {
  id: string;
  action_type: string;
  host: string;
  decision: "ask" | "allow";
  created_at: string;
}

export interface Sent {
  run_id: string | null;
  admitted: boolean;
  refusal_reason: string | null;
}

export interface SearchResult {
  bots: Bot[];
  messages: (BotMessage & { bot_name: string })[];
}

export interface ComputerStatus {
  reachable: boolean;
  url: string;
  ok?: boolean;
  screens?: { screen_id: string; label: string; controller: string; url: string }[];
}

export type BotDraft = Pick<Bot, "name" | "label" | "description" | "instructions" | "avatar">;

const json = (method: string, body?: unknown, signal?: AbortSignal): RequestInit => ({
  method,
  headers: { "content-type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
  signal,
});

export const listBots = (signal?: AbortSignal) =>
  request<{ organization_id: string; bots: Bot[] }>(BOTS_BASE, { signal });

export const createBot = (draft: BotDraft) => request<Bot>(BOTS_BASE, json("POST", draft));

export const updateBot = (id: string, fields: Partial<Bot>) =>
  request<Bot>(`${BOTS_BASE}/${id}`, json("PATCH", fields));

export const duplicateBot = (id: string) =>
  request<Bot>(`${BOTS_BASE}/${id}/duplicate`, json("POST", {}));

/** `withHelpers` deletes every helper under the bot too; otherwise they move up a level. */
export const deleteBot = (id: string, withHelpers = false) =>
  request<{ deleted: string[] }>(`${BOTS_BASE}/${id}?with_helpers=${withHelpers}`, {
    method: "DELETE",
  });

export const markRead = (id: string, unread = false) =>
  request<unknown>(`${BOTS_BASE}/${id}/read`, json("POST", { unread }));

export const stopBot = (id: string) =>
  request<unknown>(`${BOTS_BASE}/${id}/stop`, json("POST", {}));

export const fetchMessages = (id: string, after: number, signal?: AbortSignal) =>
  request<MessagePage>(`${BOTS_BASE}/${id}/messages?after=${after}`, { signal });

export const sendMessage = (id: string, text: string, replyTo?: string | null) =>
  request<Sent>(
    `${BOTS_BASE}/${id}/messages`,
    json("POST", { text, reply_to: replyTo ?? null }),
  );

export const decide = (id: string, pendingId: string, decision: "once" | "always" | "deny") =>
  request<Sent>(`${BOTS_BASE}/${id}/pending/${pendingId}`, json("POST", { decision }));

export const listRules = (id: string, signal?: AbortSignal) =>
  request<{ rules: BotRule[] }>(`${BOTS_BASE}/${id}/rules`, { signal });

export const putRule = (id: string, rule: Pick<BotRule, "action_type" | "host" | "decision">) =>
  request<{ id: string }>(`${BOTS_BASE}/${id}/rules`, json("POST", rule));

export const deleteRule = (id: string, ruleId: string) =>
  request<unknown>(`${BOTS_BASE}/${id}/rules/${ruleId}`, { method: "DELETE" });

export const searchBots = (q: string, signal?: AbortSignal) =>
  request<SearchResult>(`${BOTS_BASE}/search?q=${encodeURIComponent(q)}`, { signal });

export const screenshotUrl = (id: string, nonce: number) =>
  `${BOTS_BASE}/${id}/computer/screenshot?quality=60&t=${nonce}`;

export const setController = (id: string, controller: "bot" | "human") =>
  request<unknown>(`${BOTS_BASE}/${id}/computer/control`, json("POST", { controller }));

export type HumanInput =
  | { kind: "click"; x: number; y: number }
  | { kind: "type"; text: string }
  | { kind: "key"; key: string }
  | { kind: "scroll"; dy: number }
  | { kind: "navigate"; url: string }
  | { kind: "back" | "forward" | "reload" };

export const sendInput = (id: string, input: HumanInput) =>
  request<{ ok: boolean; url: string }>(
    `${BOTS_BASE}/${id}/computer/input`,
    json("POST", input),
  );

export const computerStatus = (signal?: AbortSignal) =>
  request<ComputerStatus>(COMPUTER_BASE, { signal });

export const resetComputer = () => request<unknown>(`${COMPUTER_BASE}/reset`, json("POST", {}));
