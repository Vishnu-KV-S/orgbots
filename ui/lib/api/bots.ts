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
  avatar: string;
  /** The job brief — the bot's primary instruction. */
  brief: Partial<BotBrief>;
  /** Only the person may change a locked brief; bots are refused. */
  brief_locked: boolean;
  brief_rev: number;
  /** Set on the list endpoint. */
  memory_count: number | null;
  pinned: boolean;
  hidden: boolean;
  unread: boolean;
  needs_attention: boolean;
  working: boolean;
  run_status: string | null;
  last_run_id: string | null;
  duplicated_from: string | null;
  parent_bot_id: string | null;
  /** The team whose files this bot shares: a bot and every helper under it. */
  team_id: string;
  created_by: "person" | "bot";
  /** The 3D body; `{}` means "derive one from the id". See `features/bots/avatar`. */
  appearance: Partial<import("@/features/bots/avatar/appearance").Appearance>;
  created_at: string;
  updated_at: string;
  last_message: BotMessage | null;
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

export const briefOf = (bot: Pick<Bot, "brief">): BotBrief => ({ ...EMPTY_BRIEF, ...bot.brief });

export interface BriefRevision {
  rev: number;
  brief: Partial<BotBrief>;
  editor_kind: "person" | "self" | "parent";
  editor_bot_id: string | null;
  editor_name: string;
  reason: string;
  changed: string[];
  created_at: string;
}

export type MemoryKind = "preference" | "person" | "fact" | "skill" | "episode";

export interface BotMemory {
  id: string;
  /** The short id the bot sees, e.g. `a1b2c3`. */
  handle: string;
  kind: MemoryKind;
  content: string;
  importance: number;
  pinned: boolean;
  source_kind: "self" | "person" | "parent" | "system";
  source_name: string;
  recall_count: number;
  last_recalled_at: string | null;
  created_at: string;
  updated_at: string;
}

export type MessageRole =
  | "user"
  | "bot"
  | "activity"
  | "approval"
  | "system"
  | "error"
  | "credentials";

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

/** One field a credential card asks for. Read off the page by the runtime. */
export interface CredentialField {
  key: string;
  kind: CredentialKind;
  label: string;
}

/** A saved login, as any list shows it: site and hint, never a value. */
export interface VaultEntry {
  id: string;
  host: string;
  label: string;
  kinds: string[];
  auto_use: boolean;
  use_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
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
  /** create_bot / ask_bot / bot_answer: the helper's name. Memory and brief steps:
   * the helper whose memory or brief changed, or empty for the bot's own. */
  bot?: string;
  label?: string;
  /** sign_in: how the form was filled — a saved login, or what the person entered. */
  via?: "saved" | "once";
  fields?: string[];
  /** File steps: the path in the team files; `to` is where a move put it. */
  path?: string;
  to?: string;
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
    outcome?: string;
    changed?: string[];
    found?: number;
    /** role "credentials": the card. Never carries a value. */
    credential_request_id?: string;
    host?: string;
    page_url?: string;
    purpose?: "sign_in" | "sign_up" | "verify";
    fields?: CredentialField[];
    saved?: { id: string; label: string }[] | boolean;
    retry?: boolean;
    /** File steps: the file and the version the step left it at. */
    file_id?: string;
    version?: number;
    chars?: number;
  };
  run_id: string | null;
  reply_to: string | null;
  created_at: string;
}

export interface MessagePage {
  messages: BotMessage[];
  pending: string[];
  /** Credential cards still waiting for the person. */
  credential_requests: string[];
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
  screens?: {
    screen_id: string;
    label: string;
    controller: string;
    url: string;
  }[];
}

export type BotDraft = Pick<Bot, "name" | "label" | "description" | "avatar"> & {
  brief: BotBrief;
  appearance?: import("@/features/bots/avatar/appearance").Appearance;
};

export type BotPatch = Partial<
  Pick<Bot, "name" | "label" | "description" | "avatar" | "pinned" | "hidden" | "brief_locked">
> & {
  brief?: BotBrief;
  brief_reason?: string;
  appearance?: import("@/features/bots/avatar/appearance").Appearance;
};

const json = (method: string, body?: unknown, signal?: AbortSignal): RequestInit => ({
  method,
  headers: { "content-type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
  signal,
});

export const listBots = (signal?: AbortSignal) =>
  request<{ organization_id: string; bots: Bot[] }>(BOTS_BASE, { signal });

export const createBot = (draft: BotDraft) => request<Bot>(BOTS_BASE, json("POST", draft));

export const updateBot = (id: string, fields: BotPatch) =>
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
  request<MessagePage>(`${BOTS_BASE}/${id}/messages?after=${after}`, {
    signal,
  });

export const sendMessage = (id: string, text: string, replyTo?: string | null) =>
  request<Sent>(`${BOTS_BASE}/${id}/messages`, json("POST", { text, reply_to: replyTo ?? null }));

export const decide = (id: string, pendingId: string, decision: "once" | "always" | "deny") =>
  request<Sent>(`${BOTS_BASE}/${id}/pending/${pendingId}`, json("POST", { decision }));

/**
 * Answer a credential card. The values go in this one request body and nowhere else —
 * not into state the caller keeps, not into a URL. The server seals them into the
 * vault; the bot is told the form was filled, never with what.
 */
export const submitCredentials = (
  id: string,
  requestId: string,
  values: Record<string, string>,
  save: boolean,
) =>
  request<Sent>(`${BOTS_BASE}/${id}/credentials/${requestId}`, json("POST", { values, save }));

export const chooseSavedLogin = (id: string, requestId: string, entryId: string) =>
  request<Sent>(
    `${BOTS_BASE}/${id}/credentials/${requestId}`,
    json("POST", { use_entry_id: entryId }),
  );

export const cancelCredentials = (id: string, requestId: string) =>
  request<Sent>(`${BOTS_BASE}/${id}/credentials/${requestId}/cancel`, json("POST", {}));

export const VAULT_BASE = "/rt/v1/vault";

export const listVault = (signal?: AbortSignal) =>
  request<{ entries: VaultEntry[] }>(VAULT_BASE, { signal });

export const setVaultAutoUse = (entryId: string, autoUse: boolean) =>
  request<unknown>(`${VAULT_BASE}/${entryId}`, json("PATCH", { auto_use: autoUse }));

export const deleteVaultEntry = (entryId: string) =>
  request<unknown>(`${VAULT_BASE}/${entryId}`, { method: "DELETE" });

export const listRules = (id: string, signal?: AbortSignal) =>
  request<{ rules: BotRule[] }>(`${BOTS_BASE}/${id}/rules`, { signal });

export const putRule = (id: string, rule: Pick<BotRule, "action_type" | "host" | "decision">) =>
  request<{ id: string }>(`${BOTS_BASE}/${id}/rules`, json("POST", rule));

export const deleteRule = (id: string, ruleId: string) =>
  request<unknown>(`${BOTS_BASE}/${id}/rules/${ruleId}`, { method: "DELETE" });

export const listRevisions = (id: string, signal?: AbortSignal) =>
  request<{ revisions: BriefRevision[] }>(`${BOTS_BASE}/${id}/brief/revisions`, { signal });

export const restoreRevision = (id: string, rev: number) =>
  request<Bot>(`${BOTS_BASE}/${id}/brief/revisions/${rev}/restore`, json("POST", {}));

export const listMemories = (id: string, signal?: AbortSignal) =>
  request<{ memories: BotMemory[] }>(`${BOTS_BASE}/${id}/memories`, { signal });

export const addMemory = (
  id: string,
  memory: Pick<BotMemory, "content" | "kind" | "importance" | "pinned">,
) => request<{ id: string }>(`${BOTS_BASE}/${id}/memories`, json("POST", memory));

export const editMemory = (
  id: string,
  memoryId: string,
  fields: Partial<Pick<BotMemory, "content" | "kind" | "importance" | "pinned">>,
) => request<unknown>(`${BOTS_BASE}/${id}/memories/${memoryId}`, json("PATCH", fields));

export const deleteMemory = (id: string, memoryId: string) =>
  request<unknown>(`${BOTS_BASE}/${id}/memories/${memoryId}`, { method: "DELETE" });

export const clearMemories = (id: string) =>
  request<{ deleted: number }>(`${BOTS_BASE}/${id}/memories`, { method: "DELETE" });

export const searchBots = (q: string, signal?: AbortSignal) =>
  request<SearchResult>(`${BOTS_BASE}/search?q=${encodeURIComponent(q)}`, {
    signal,
  });

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
  request<{ ok: boolean; url: string }>(`${BOTS_BASE}/${id}/computer/input`, json("POST", input));

export const computerStatus = (signal?: AbortSignal) =>
  request<ComputerStatus>(COMPUTER_BASE, { signal });

export const resetComputer = () => request<unknown>(`${COMPUTER_BASE}/reset`, json("POST", {}));

/** A file in a team's shared drive. Listings carry no `content`; one file does. */
export interface TeamFile {
  id: string;
  path: string;
  name: string;
  folder: string;
  chars: number;
  version: number;
  /** Only the person may change a locked file; bots are refused. */
  locked: boolean;
  created_by_kind: "person" | "bot";
  created_by_name: string;
  updated_by_kind: "person" | "bot";
  updated_by_bot_id: string | null;
  updated_by_name: string;
  created_at: string;
  updated_at: string;
  /** Set when the file is in the trash. */
  deleted_at: string | null;
  content?: string;
  /** A search match: the text around the first hit. */
  snippet?: string;
}

export interface TeamMember {
  id: string;
  name: string;
  label: string;
  parent_bot_id: string | null;
}

export interface TeamFiles {
  team: { id: string; members: TeamMember[] };
  files: TeamFile[];
  trash: TeamFile[];
  /** Only when searched. */
  matches?: TeamFile[];
}

export interface FileRevision {
  id: string;
  version: number;
  op: "create" | "write" | "append" | "edit" | "move" | "delete" | "restore";
  path: string;
  content: string;
  editor_kind: "person" | "bot";
  editor_name: string;
  run_id: string | null;
  note: string;
  created_at: string;
}

export type FileChange = { file: TeamFile; op: string };

const filesOf = (id: string) => `${BOTS_BASE}/${id}/files`;

/** The drive of the team `id` is on — any bot on the team gives the same drive. */
export const listFiles = (id: string, q = "", signal?: AbortSignal) =>
  request<TeamFiles>(`${filesOf(id)}${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`, {
    signal,
  });

export const getFile = (id: string, fileId: string, signal?: AbortSignal) =>
  request<TeamFile>(`${filesOf(id)}/${fileId}`, { signal });

export const createFile = (id: string, path: string, content: string) =>
  request<FileChange>(filesOf(id), json("POST", { path, content }));

/** Save new content. `baseVersion` is the version that was opened: saving over a newer
 * change (a bot's, say) is refused with a 409 rather than silently undoing it. */
export const saveFile = (id: string, fileId: string, content: string, baseVersion: number) =>
  request<FileChange>(
    `${filesOf(id)}/${fileId}`,
    json("PATCH", { content, base_version: baseVersion }),
  );

/** Rename, or move into a folder (a path ending in `/`). */
export const moveFile = (id: string, fileId: string, path: string) =>
  request<FileChange>(`${filesOf(id)}/${fileId}`, json("PATCH", { path }));

export const lockFile = (id: string, fileId: string, locked: boolean) =>
  request<{ file: TeamFile }>(`${filesOf(id)}/${fileId}`, json("PATCH", { locked }));

export const deleteFile = (id: string, fileId: string, baseVersion?: number) =>
  request<FileChange>(
    `${filesOf(id)}/${fileId}${baseVersion ? `?base_version=${baseVersion}` : ""}`,
    { method: "DELETE" },
  );

export const listFileRevisions = (id: string, fileId: string, signal?: AbortSignal) =>
  request<{ revisions: FileRevision[] }>(`${filesOf(id)}/${fileId}/revisions`, { signal });

/** Out of the trash, or back to an earlier revision's content (as a new revision). */
export const restoreFile = (id: string, fileId: string, version?: number) =>
  request<FileChange>(`${filesOf(id)}/${fileId}/restore`, json("POST", { version }));
