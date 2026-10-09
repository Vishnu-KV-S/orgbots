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
  /** A second model checks this bot's risky steps against what was asked. */
  auto_review: boolean;
  /** With members: whose bot this is (null: everyone's), and whether it is shared. */
  owner_member_id: string | null;
  visibility: "private" | "team";
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
  /** save_routine / delete_routine: the routine's name. */
  name?: string;
  /** run_command: on the person's own computer rather than the sandbox. */
  local?: boolean;
  /** An Auto Review line: what the reviewer decided. */
  verdict?: "allow" | "ask" | "deny";
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
    /** Which member wrote a user message, when the runtime has members. */
    from?: { member_id: string; name: string };
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
    /** Files sent with a person's message — already in the team's drive. */
    attachments?: Attachment[];
    /** Said in a voice chat; and a voice chat's card (role "system"). */
    voice?: boolean;
    voice_call?: { seconds: number; turns: number };
    /** A demonstration's message (role "user"): the goal, and how many steps. */
    demonstration?: string;
    recording_id?: string;
    steps?: number;
    teach?: "started" | "cancelled";
    /** A message from another of the person's bots (not the bot's creator). */
    peer?: boolean;
    handoff?: boolean;
    /** A reply posted in a group, and the teammates it handed parts to. */
    group_id?: string;
    group_name?: string;
    handed_to?: string[];
    /** run_command's result: the tail of what it printed. */
    output?: string;
    exit_code?: number | null;
    mode?: "sandbox" | "local";
    timed_out?: boolean;
    /** A message a routine sent (role "user"), and save_routine's result. */
    routine?: string;
    routine_id?: string;
    trigger?: RoutineTrigger;
    schedule?: string;
    active?: boolean;
    next_fire_at?: string | null;
  };
  run_id: string | null;
  reply_to: string | null;
  created_at: string;
  /** The person's reactions, kept on the server. */
  reactions?: string[];
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
  /** "deny" is never allow: the step is refused, not asked about. */
  decision: "ask" | "allow" | "deny";
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
  Pick<
    Bot,
    | "name"
    | "label"
    | "description"
    | "avatar"
    | "pinned"
    | "hidden"
    | "brief_locked"
    | "auto_review"
    | "visibility"
  >
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

export interface Attachment {
  id: string;
  path: string;
  name: string;
  media_type: string;
  bytes: number;
  kind: string;
  chars?: number;
}

export const sendMessage = (
  id: string,
  text: string,
  replyTo?: string | null,
  attachments: string[] = [],
  { voice = false }: { voice?: boolean } = {},
) =>
  request<Sent>(
    `${BOTS_BASE}/${id}/messages`,
    json("POST", { text, reply_to: replyTo ?? null, attachments, voice }),
  );

/** A voice chat ended: a card in the conversation with how long it was. */
export const recordVoiceCall = (id: string, seconds: number, turns: number) =>
  request<{ message_id: string }>(
    `${BOTS_BASE}/${id}/voice-calls`,
    json("POST", { seconds, turns }),
  );

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

/** `botId` picks that bot's browser profile — with members, a team bot's logins are its
 * own; without it, the caller's. */
const forBot = (botId?: string) => (botId ? `bot_id=${botId}` : "");

export const listVault = (signal?: AbortSignal, botId?: string) =>
  request<{ entries: VaultEntry[] }>(`${VAULT_BASE}?${forBot(botId)}`, { signal });

export const setVaultAutoUse = (entryId: string, autoUse: boolean, botId?: string) =>
  request<unknown>(
    `${VAULT_BASE}/${entryId}?${forBot(botId)}`,
    json("PATCH", { auto_use: autoUse }),
  );

export const deleteVaultEntry = (entryId: string, botId?: string) =>
  request<unknown>(`${VAULT_BASE}/${entryId}?${forBot(botId)}`, { method: "DELETE" });

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
  /** An image, a PDF or a document keeps its bytes; `content` is then the text read
   * out of it (empty for an image), and it cannot be edited as text. */
  media_type: string;
  bytes: number;
  binary: boolean;
  /** "text", "image", "PDF", "Word document", "spreadsheet", "presentation" or "file". */
  kind: string;
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

/** A file's bytes — an image to show, a PDF to open, anything to download. */
export const rawFileUrl = (id: string, fileId: string) => `${filesOf(id)}/${fileId}/raw`;

const base64Of = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error ?? new Error(`could not read ${file.name}`));
    reader.readAsDataURL(file);
  });

/** Upload files into the team's drive — a message's attachments (default folder
 * `/attachments/<today>`) or the Files pane's uploads. Any type: the runtime decides
 * from the bytes whether it is text, and reads the text out of PDFs and documents. */
export const uploadFiles = async (id: string, files: File[], folder?: string) =>
  request<{ files: TeamFile[] }>(
    `${filesOf(id)}/upload`,
    json("POST", {
      folder: folder || undefined,
      files: await Promise.all(
        files.map(async (f) => ({ name: f.name, media_type: f.type, data: await base64Of(f) })),
      ),
    }),
  );

export const listFileRevisions = (id: string, fileId: string, signal?: AbortSignal) =>
  request<{ revisions: FileRevision[] }>(`${filesOf(id)}/${fileId}/revisions`, { signal });

/** Out of the trash, or back to an earlier revision's content (as a new revision). */
export const restoreFile = (id: string, fileId: string, version?: number) =>
  request<FileChange>(`${filesOf(id)}/${fileId}/restore`, json("POST", { version }));

// --- routines -------------------------------------------------------------------------

export type RoutineTrigger = "schedule" | "event" | "test";

export interface EventMatch {
  events: string[];
  contains: string;
  actor: string;
}

export interface RoutineRun {
  id: string;
  trigger: RoutineTrigger;
  status: "queued" | "started" | "refused" | "skipped" | "missed";
  scheduled_for: string | null;
  run_id: string | null;
  detail: string;
  event: { source?: string; name?: string; actor?: string; text?: string; url?: string };
  created_at: string;
  started_at: string | null;
}

export interface Routine {
  id: string;
  bot_id: string;
  name: string;
  instruction: string;
  kind: "schedule" | "event";
  cron: string | null;
  timezone: string;
  /** The schedule as a person says it ("Weekdays at 08:00"); empty for an event routine. */
  schedule: string;
  source: "webhook" | "github" | "slack" | null;
  match: Partial<EventMatch>;
  /** Where the sender posts events. Whoever has it can start the routine. */
  hook_url: string | null;
  has_secret: boolean;
  inputs: string;
  output: string;
  approval: "default" | "drafts";
  when_missing: string;
  active: boolean;
  created_by_kind: "person" | "bot";
  next_fire_at: string | null;
  last_fired_at: string | null;
  fire_count: number;
  last_run: RoutineRun | null;
  created_at: string;
  updated_at: string;
}

export type RoutineDraft = Pick<
  Routine,
  | "name"
  | "instruction"
  | "kind"
  | "cron"
  | "timezone"
  | "source"
  | "inputs"
  | "output"
  | "approval"
  | "when_missing"
  | "active"
> & { match: EventMatch; signing_secret?: string };

const routinesOf = (id: string) => `${BOTS_BASE}/${id}/routines`;

export const listRoutines = (id: string, signal?: AbortSignal) =>
  request<{ routines: Routine[] }>(routinesOf(id), { signal });

export const createRoutine = (id: string, draft: RoutineDraft) =>
  request<Routine>(routinesOf(id), json("POST", draft));

export const updateRoutine = (id: string, routineId: string, fields: Partial<RoutineDraft>) =>
  request<Routine>(`${routinesOf(id)}/${routineId}`, json("PATCH", fields));

export const deleteRoutine = (id: string, routineId: string) =>
  request<{ deleted: string }>(`${routinesOf(id)}/${routineId}`, { method: "DELETE" });

/** Run it now, drafts only — like a message, it interrupts whatever the bot is doing. */
export const testRoutine = (id: string, routineId: string) =>
  request<Sent & { fire_id: string }>(`${routinesOf(id)}/${routineId}/test`, json("POST", {}));

export const listRoutineRuns = (id: string, routineId: string, signal?: AbortSignal) =>
  request<{ runs: RoutineRun[] }>(`${routinesOf(id)}/${routineId}/runs`, { signal });

// --- skills ---------------------------------------------------------------------------

export const SKILLS_BASE = "/rt/v1/skills";
export const MARKETPLACE_BASE = "/rt/v1/marketplace";

export interface SkillFields {
  title: string;
  /** When to use it. */
  when: string;
  inputs: string;
  steps: string[];
  checks: string;
  output: string;
  approvals: string;
}

export interface Skill extends SkillFields {
  id: string;
  /** Called as `/name` in a message. */
  name: string;
  /** A draft is not offered to bots until a person marks it ready. */
  status: "draft" | "ready";
  source: "person" | "bot" | "demonstration" | "marketplace";
  source_bot_id: string | null;
  recording_id: string | null;
  version: number;
  updated_by_kind: "person" | "bot";
  updated_by_name: string;
  use_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MarketSkill extends SkillFields {
  key: string;
  name: string;
  category: string;
  blurb: string;
  installed: boolean;
}

export const EMPTY_SKILL: SkillFields = {
  title: "",
  when: "",
  inputs: "",
  steps: [],
  checks: "",
  output: "",
  approvals: "",
};

export const listSkills = (signal?: AbortSignal) =>
  request<{ skills: Skill[] }>(SKILLS_BASE, { signal });

export const createSkill = (fields: SkillFields & { name?: string; status?: Skill["status"] }) =>
  request<Skill>(SKILLS_BASE, json("POST", fields));

export const updateSkill = (
  id: string,
  fields: Partial<SkillFields> & { name?: string; status?: Skill["status"] },
) => request<Skill>(`${SKILLS_BASE}/${id}`, json("PATCH", fields));

export const deleteSkill = (id: string) =>
  request<{ deleted: string }>(`${SKILLS_BASE}/${id}`, { method: "DELETE" });

export const listMarketplace = (signal?: AbortSignal) =>
  request<{ skills: MarketSkill[] }>(MARKETPLACE_BASE, { signal });

export const installSkill = (key: string) =>
  request<Skill>(`${MARKETPLACE_BASE}/skills/${key}`, json("POST", {}));

export interface RecordedStep {
  kind: string;
  url?: string;
  text?: string;
  key?: string;
  target?: { label?: string; role?: string; tag?: string; secret?: boolean } | null;
}

export interface Teaching {
  recording: {
    id: string;
    goal: string;
    status: string;
    steps: RecordedStep[];
    started_at: string;
  } | null;
  computer?: {
    recording: boolean;
    steps?: number;
    elapsed?: number;
    full?: boolean;
    last?: RecordedStep | null;
  };
}

/** The demonstration in progress on this bot's screen, if any. */
export const teachingStatus = (botId: string, signal?: AbortSignal) =>
  request<Teaching>(`${BOTS_BASE}/${botId}/teach`, { signal });

/** Hand the screen to the person and start recording what they do. */
export const startTeaching = (botId: string, goal: string) =>
  request<{ recording_id: string }>(`${BOTS_BASE}/${botId}/teach`, json("POST", { goal }));

/** Stop recording and give the screen back. Unless cancelled, the bot gets the recording
 * and writes it up as a draft skill. */
export const stopTeaching = (botId: string, cancel = false) =>
  request<{ status: "stopped" | "cancelled"; steps: number; run_id?: string | null }>(
    `${BOTS_BASE}/${botId}/teach/stop`,
    json("POST", { cancel }),
  );

// --- the computer's workspace and terminal ---------------------------------------------

export interface WorkspaceEntry {
  name: string;
  /** Always starts with /workspace. */
  path: string;
  folder: boolean;
  link?: boolean;
  bytes: number;
  modified: number;
}

export interface CommandResult {
  ok: boolean;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  truncated: boolean;
  timed_out: boolean;
  seconds: number;
  mode: "sandbox" | "local";
}

/** The bots' shared /workspace on the computer. */
export const listWorkspace = (path = "/workspace", signal?: AbortSignal, botId?: string) =>
  request<{ path: string; entries: WorkspaceEntry[] }>(
    `${COMPUTER_BASE}/workspace?path=${encodeURIComponent(path)}&${forBot(botId)}`,
    { signal },
  );

export const workspaceFileUrl = (path: string, botId?: string) =>
  `${COMPUTER_BASE}/workspace/file?path=${encodeURIComponent(path)}&${forBot(botId)}`;

export const uploadToWorkspace = async (folder: string, file: File, botId?: string) =>
  request<{ path: string; bytes: number }>(
    `${COMPUTER_BASE}/workspace/file?${forBot(botId)}`,
    json("POST", {
      path: `${folder.replace(/\/+$/, "")}/${file.name}`,
      data: await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
        reader.onerror = () => reject(reader.error ?? new Error(`could not read ${file.name}`));
        reader.readAsDataURL(file);
      }),
    }),
  );

/** The person's own command — always in the sandbox, never on this machine. */
export const runSandboxCommand = (command: string, timeoutS = 60, botId?: string) =>
  request<CommandResult>(
    `${COMPUTER_BASE}/terminal?${forBot(botId)}`,
    json("POST", { command, timeout_s: timeoutS }),
  );

// --- reactions -------------------------------------------------------------------------

export const REACTIONS = ["👍", "👎", "❤️", "🎉", "✅", "👀", "😂", "🙏"] as const;

export const reactToBotMessage = (botId: string, messageId: string, emoji: string, on: boolean) =>
  request<{ message_id: string; reactions: string[] }>(
    `${BOTS_BASE}/${botId}/messages/${messageId}/reactions`,
    json("POST", { emoji, on }),
  );

// --- group chats -----------------------------------------------------------------------

export const GROUPS_BASE = "/rt/v1/groups";

export interface GroupMember {
  id: string;
  name: string;
  label: string;
  working: boolean;
}

export interface GroupMessage {
  id: string;
  seq: number;
  group_id: string;
  author_kind: "person" | "bot" | "system";
  author_bot_id: string | null;
  author_name: string;
  content: string;
  payload: Record<string, unknown>;
  /** Set on a reply in a thread: the message the thread hangs off. */
  thread_root: string | null;
  run_id: string | null;
  created_at: string;
  reactions: string[];
}

export interface Group {
  id: string;
  name: string;
  /** Answers a message that names nobody. */
  lead_bot_id: string | null;
  members: GroupMember[];
  unread: boolean;
  created_at: string;
  updated_at: string;
  last_message: GroupMessage | null;
}

export const listGroups = (signal?: AbortSignal) =>
  request<{ groups: Group[] }>(GROUPS_BASE, { signal });

export const createGroup = (name: string, members: string[], lead?: string | null) =>
  request<Group>(GROUPS_BASE, json("POST", { name, members, lead: lead ?? null }));

export const updateGroup = (
  id: string,
  fields: { name?: string; members?: string[]; lead?: string | null },
) => request<Group>(`${GROUPS_BASE}/${id}`, json("PATCH", fields));

export const deleteGroup = (id: string) =>
  request<{ deleted: string }>(`${GROUPS_BASE}/${id}`, { method: "DELETE" });

export const groupMessages = (id: string, after: number, signal?: AbortSignal) =>
  request<{ messages: GroupMessage[]; working: string[] }>(
    `${GROUPS_BASE}/${id}/messages?after=${after}`,
    { signal },
  );

/** Post to the group. The bots it @names — or the lead, if it names nobody — start on it. */
export const postToGroup = (id: string, text: string, threadRoot?: string | null) =>
  request<{ message_id: string; runs: Sent[] }>(
    `${GROUPS_BASE}/${id}/messages`,
    json("POST", { text, thread_root: threadRoot ?? null }),
  );

export const markGroupRead = (id: string) =>
  request<unknown>(`${GROUPS_BASE}/${id}/read`, json("POST", {}));

export const reactInGroup = (id: string, messageId: string, emoji: string, on: boolean) =>
  request<{ message_id: string; reactions: string[] }>(
    `${GROUPS_BASE}/${id}/messages/${messageId}/reactions`,
    json("POST", { emoji, on }),
  );

// --- connectors (MCP apps) ---------------------------------------------------------------

export const CONNECTORS_BASE = "/rt/v1/connectors";

export interface ConnectorTool {
  name: string;
  description: string;
  read_only: boolean;
}

export interface Connector {
  id: string;
  /** What a bot writes in `connector`. */
  name: string;
  title: string;
  url: string;
  auth_kind: "none" | "bearer" | "header";
  header_name: string;
  /** Whether a token is stored — never the token. */
  has_token: boolean;
  server_name: string;
  status: "ok" | "error";
  last_error: string;
  enabled: boolean;
  catalog_key: string | null;
  tools: ConnectorTool[];
  created_at: string;
  updated_at: string;
}

export interface MarketConnector {
  key: string;
  title: string;
  category: string;
  blurb: string;
  url: string;
  auth_kind: "none" | "bearer" | "header";
  key_help: string;
  needs_token: boolean;
  takes_token: boolean;
  /** This runtime has been connected to it, rather than only documented by its vendor. */
  checked: boolean;
  installed: boolean;
}

export const listConnectors = (signal?: AbortSignal) =>
  request<{ connectors: Connector[] }>(CONNECTORS_BASE, { signal });

export const addConnector = (fields: {
  title: string;
  url: string;
  auth_kind: Connector["auth_kind"];
  header_name?: string;
  token?: string;
}) => request<Connector>(CONNECTORS_BASE, json("POST", fields));

export const updateConnector = (
  id: string,
  fields: { title?: string; enabled?: boolean; token?: string },
) => request<Connector>(`${CONNECTORS_BASE}/${id}`, json("PATCH", fields));

export const refreshConnector = (id: string) =>
  request<Connector>(`${CONNECTORS_BASE}/${id}/refresh`, json("POST", {}));

export const deleteConnector = (id: string) =>
  request<{ deleted: string }>(`${CONNECTORS_BASE}/${id}`, { method: "DELETE" });

export const listMarketConnectors = (signal?: AbortSignal) =>
  request<{ connectors: MarketConnector[] }>(`${MARKETPLACE_BASE}/connectors`, { signal });

export const installConnector = (key: string, token?: string) =>
  request<Connector>(
    `${MARKETPLACE_BASE}/connectors/${key}`,
    json("POST", token ? { token } : {}),
  );

// --- push notifications -----------------------------------------------------------------

export const PUSH_BASE = "/rt/v1/push";

export const pushKey = () => request<{ public_key: string }>(PUSH_BASE, {});

export const pushSubscribe = (subscription: PushSubscriptionJSON, device: string) =>
  request<{ subscribed: boolean }>(
    `${PUSH_BASE}/subscribe`,
    json("POST", { ...subscription, device }),
  );

export const pushUnsubscribe = (endpoint: string) =>
  request<{ subscribed: boolean }>(`${PUSH_BASE}/unsubscribe`, json("POST", { endpoint }));

export const pushTest = () => request<{ queued: boolean }>(`${PUSH_BASE}/test`, json("POST", {}));


// --- templates --------------------------------------------------------------------------

export const TEMPLATES_BASE = "/rt/v1/templates";

/** A bot's setup as a file or a link: never its memories, conversation, sign-ins or files. */
export interface BotTemplate {
  format: "agent-org/bot-template";
  version: number;
  name: string;
  label: string;
  description: string;
  avatar: string;
  appearance: import("@/features/bots/avatar/appearance").Appearance | null;
  brief: BotBrief;
  auto_review: boolean;
  rules: TemplateRule[];
  routines: { name: string; instruction: string; kind: string; cron: string | null }[];
}

export interface TemplateRule {
  action_type: string;
  host: string;
  decision: "ask" | "allow" | "deny";
}

/** What importing will do: the rules it writes, the allow rules it leaves out unless
 * asked, and every routine — paused. */
export interface TemplatePreview {
  template: BotTemplate;
  file_name: string;
  plan: {
    rules: TemplateRule[];
    allows: TemplateRule[];
    routines: { name: string; kind: string; when: string; instruction: string }[];
  };
  uses?: number;
}

export interface TemplateLink {
  id: string;
  token: string;
  name: string;
  uses: number;
  created_at: string;
}

export const exportTemplate = (botId: string) =>
  request<{ template: BotTemplate; file_name: string }>(`${BOTS_BASE}/${botId}/template`, {});

export const listTemplateLinks = (botId: string, signal?: AbortSignal) =>
  request<{ links: TemplateLink[] }>(`${BOTS_BASE}/${botId}/template-links`, { signal });

export const makeTemplateLink = (botId: string) =>
  request<TemplateLink>(`${BOTS_BASE}/${botId}/template-links`, json("POST", {}));

export const revokeTemplateLink = (botId: string, linkId: string) =>
  request<{ revoked: string }>(`${BOTS_BASE}/${botId}/template-links/${linkId}`, {
    method: "DELETE",
  });

export const previewTemplate = (template: unknown) =>
  request<TemplatePreview>(`${TEMPLATES_BASE}/preview`, json("POST", { template }));

export const sharedTemplate = (token: string, signal?: AbortSignal) =>
  request<TemplatePreview>(`${TEMPLATES_BASE}/shared/${encodeURIComponent(token)}`, { signal });

export const importTemplate = (
  source: { template: BotTemplate } | { token: string },
  options: { name?: string; keep_allows: boolean },
) => request<Bot>(`${TEMPLATES_BASE}/import`, json("POST", { ...source, ...options }));

// --- members and sign-in ----------------------------------------------------------------

export const AUTH_BASE = "/rt/v1/auth";
export const MEMBERS_BASE = "/rt/v1/members";

export type Role = "owner" | "admin" | "member";

export interface Me {
  id: string;
  email: string;
  name: string;
  role: Role;
  organization_id: string;
}

export interface TeamMember {
  id: string;
  email: string;
  name: string;
  role: Role;
  active: boolean;
  created_at: string;
  last_seen_at: string | null;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  created_at: string;
  expires_at: string;
  link?: string;
}

export interface SSOConfig {
  configured: boolean;
  callback_url: string;
  issuer?: string;
  client_id?: string;
  has_secret?: boolean;
  domains?: string[];
  auto_join?: boolean;
  enabled?: boolean;
}

export const authMe = (signal?: AbortSignal) =>
  request<{ mode: "none" | "members"; member: Me | null }>(`${AUTH_BASE}/me`, { signal });

export const linkInfo = (token: string) =>
  request<{
    kind: "invite" | "sign_in";
    email: string;
    role: Role;
    organization: string;
    joining: boolean;
  }>(`${AUTH_BASE}/links/${encodeURIComponent(token)}`, {});

export const acceptLink = (token: string, name: string) =>
  request<{ member: Me }>(
    `${AUTH_BASE}/links/${encodeURIComponent(token)}`,
    json("POST", { name }),
  );

export const startSSO = (email: string, returnTo: string) =>
  request<{ redirect_url: string }>(
    `${AUTH_BASE}/sso/start`,
    json("POST", { email, return_to: returnTo }),
  );

export const signOut = () =>
  request<{ signed_out: boolean }>(`${AUTH_BASE}/sign-out`, json("POST", {}));

export const listMembers = (signal?: AbortSignal) =>
  request<{ members: TeamMember[]; me: string }>(MEMBERS_BASE, { signal });

export const inviteMember = (email: string, role: Role) =>
  request<Invite>(`${MEMBERS_BASE}/invites`, json("POST", { email, role }));

export const listInvites = (signal?: AbortSignal) =>
  request<{ invites: Invite[] }>(`${MEMBERS_BASE}/invites`, { signal });

export const revokeInvite = (id: string) =>
  request<{ revoked: string }>(`${MEMBERS_BASE}/invites/${id}`, { method: "DELETE" });

export const updateMember = (id: string, change: { role?: Role; active?: boolean }) =>
  request<TeamMember>(`${MEMBERS_BASE}/${id}`, json("PATCH", change));

export const memberSignInLink = (id: string) =>
  request<{ link: string }>(`${MEMBERS_BASE}/${id}/sign-in-link`, json("POST", {}));

export const getSSO = (signal?: AbortSignal) =>
  request<SSOConfig>(`${MEMBERS_BASE}/sso`, { signal });

export const saveSSO = (config: {
  issuer: string;
  client_id: string;
  client_secret?: string;
  domains: string[];
  auto_join: boolean;
  enabled: boolean;
}) => request<SSOConfig>(`${MEMBERS_BASE}/sso`, json("PUT", config));

export const deleteSSO = () => request<SSOConfig>(`${MEMBERS_BASE}/sso`, { method: "DELETE" });

// --- organization admin: policies, secrets, provisioning, telemetry, audit ---------------

export const ADMIN_BASE = "/rt/v1/admin";

export interface OrgPolicy {
  network: "open" | "allowlist";
  allowed_hosts: string[];
  require_review: boolean;
  template_links: boolean;
  members_add_apps: boolean;
}

export interface TeamSecret {
  name: string;
  bytes: number;
  updated_at: string;
}

export interface ScimStatus {
  base_url: string;
  configured: boolean;
  hint: string | null;
  last_used_at: string | null;
}

export interface Telemetry {
  configured: boolean;
  endpoint?: string;
  has_headers?: boolean;
  include_email?: boolean;
  include_actions?: boolean;
  enabled?: boolean;
  last_error?: string;
  last_sent_at?: string | null;
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

export const getPolicy = (signal?: AbortSignal) =>
  request<OrgPolicy>(`${ADMIN_BASE}/policy`, { signal });

export const savePolicy = (policy: OrgPolicy) =>
  request<OrgPolicy>(`${ADMIN_BASE}/policy`, json("PUT", policy));

export const listSecrets = (signal?: AbortSignal) =>
  request<{ secrets: TeamSecret[] }>(`${ADMIN_BASE}/secrets`, { signal });

export const putSecret = (name: string, value: string) =>
  request<{ saved: boolean }>(
    `${ADMIN_BASE}/secrets/${encodeURIComponent(name)}`,
    json("PUT", { value }),
  );

export const deleteSecret = (name: string) =>
  request<{ deleted: string }>(`${ADMIN_BASE}/secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });

export const scimStatus = (signal?: AbortSignal) =>
  request<ScimStatus>(`${ADMIN_BASE}/scim`, { signal });

export const makeScimToken = () =>
  request<{ token: string; base_url: string }>(`${ADMIN_BASE}/scim/token`, json("POST", {}));

export const revokeScim = () =>
  request<{ configured: boolean }>(`${ADMIN_BASE}/scim`, { method: "DELETE" });

export const getTelemetry = (signal?: AbortSignal) =>
  request<Telemetry>(`${ADMIN_BASE}/telemetry`, { signal });

export const saveTelemetry = (config: {
  endpoint: string;
  headers?: Record<string, string>;
  clear_headers?: boolean;
  include_email: boolean;
  include_actions: boolean;
  enabled: boolean;
}) => request<Telemetry>(`${ADMIN_BASE}/telemetry`, json("PUT", config));

export const deleteTelemetry = () =>
  request<Telemetry>(`${ADMIN_BASE}/telemetry`, { method: "DELETE" });

export const auditEvents = (action = "", signal?: AbortSignal) =>
  request<{ events: AuditEvent[] }>(`${ADMIN_BASE}/audit?action=${encodeURIComponent(action)}`, {
    signal,
  });

// --- tag @bot on X ------------------------------------------------------------------------

export const X_BASE = "/rt/v1/x";

export interface XStatus {
  account: string | null;
  link: { handle: string; bot_id: string | null } | null;
  admin?: {
    enabled: boolean;
    can_reply: boolean;
    last_error: string;
    last_polled_at: string | null;
    recent: { post_id: string; author: string; outcome: string; note: string; at: string }[];
  };
}

export const xStatus = (signal?: AbortSignal) => request<XStatus>(X_BASE, { signal });

export const connectX = (account: {
  handle: string;
  read_token?: string;
  post_token?: string;
  clear_post?: boolean;
  enabled?: boolean;
}) => request<{ account: string; can_reply: boolean }>(`${X_BASE}/account`, json("PUT", account));

export const disconnectX = () =>
  request<{ account: null }>(`${X_BASE}/account`, { method: "DELETE" });

export const xLinkCode = () =>
  request<{ code: string; post: string; account: string }>(`${X_BASE}/link/code`, json("POST", {}));

export const xChooseBot = (botId: string) =>
  request<{ bot_id: string }>(`${X_BASE}/link`, json("PATCH", { bot_id: botId }));

export const xUnlink = () => request<{ link: null }>(`${X_BASE}/link`, { method: "DELETE" });
