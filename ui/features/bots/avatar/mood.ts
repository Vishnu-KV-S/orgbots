import type { Bot, BotMessage } from "@/lib/api/bots";

/**
 * What the bot's body is doing. Every action a bot takes maps to one of these, and
 * each has its own animation in `BotModel`.
 */
export type Mood =
  | "idle"
  | "thinking"
  | "browsing"
  | "clicking"
  | "typing"
  | "waiting"
  | "error"
  | "happy"
  | "creating"
  | "delegating"
  | "remembering"
  | "sleeping"
  | "stopped";

export const MOODS: { id: Mood; label: string; hint: string }[] = [
  { id: "idle", label: "Idle", hint: "Floats, blinks, glances around" },
  {
    id: "thinking",
    label: "Thinking",
    hint: "Tilts its head, eyes drift up",
  },
  {
    id: "browsing",
    label: "Browsing",
    hint: "Eyes scan the page left and right",
  },
  { id: "clicking", label: "Clicking", hint: "Squash-and-stretch taps" },
  { id: "typing", label: "Typing", hint: "Focused squint and a busy jitter" },
  {
    id: "waiting",
    label: "Needs you",
    hint: "Head cocked, one eye wider",
  },
  { id: "error", label: "Error", hint: "X eyes, a shudder, red LEDs" },
  { id: "happy", label: "Done", hint: "Happy eyes and a little hop" },
  { id: "creating", label: "New helper", hint: "Turns slowly, LEDs brighten" },
  {
    id: "delegating",
    label: "Asking helper",
    hint: "Leans in and nods",
  },
  {
    id: "remembering",
    label: "Remembering",
    hint: "Eyes close, LEDs brighten",
  },
  {
    id: "sleeping",
    label: "Sleeping",
    hint: "Eyes shut, slow breathing, LEDs dim",
  },
  { id: "stopped", label: "Stopped", hint: "Slumps and powers down" },
];

const ACTION_MOOD: Record<string, Mood> = {
  navigate: "browsing",
  scroll: "browsing",
  back: "browsing",
  forward: "browsing",
  reload: "browsing",
  observe: "browsing",
  hover: "browsing",
  wait: "thinking",
  click: "clicking",
  select: "clicking",
  press: "clicking",
  type: "typing",
  remember: "remembering",
  create_bot: "creating",
  ask_bot: "delegating",
  bot_answer: "happy",
};

const HOUR = 3_600_000;
const HAPPY_FOR_MS = 12_000;

/** For a list row: from the summary the bot list carries. */
export function moodOfBot(bot: Bot, now = Date.now()): Mood {
  const last = bot.last_message;
  if (bot.working) return "thinking";
  if (last?.role === "approval" || bot.needs_attention) return "waiting";
  if (last?.role === "error") return "error";
  if (last?.role === "bot" && now - Date.parse(last.created_at) < HAPPY_FOR_MS) return "happy";
  const lastActive = Date.parse(last?.created_at ?? bot.updated_at);
  if (bot.hidden || now - lastActive > 6 * HOUR) return "sleeping";
  return "idle";
}

/** For an open conversation: from the transcript, which says exactly what it is doing. */
export function moodOfConversation(
  messages: BotMessage[],
  { working, pending }: { working: boolean; pending: number },
  now = Date.now(),
): Mood {
  if (pending > 0) return "waiting";
  const last = messages[messages.length - 1];
  if (working) {
    if (!last || last.role === "user") return "thinking";
    if (last.role === "activity") {
      if (last.payload.ok === false) return "error";
      return ACTION_MOOD[last.payload.action?.type ?? ""] ?? "thinking";
    }
    return "thinking";
  }
  if (!last) return "idle";
  if (last.role === "error") return "error";
  if (last.role === "system" && /stop/i.test(last.content)) return "stopped";
  if (last.role === "system" && last.payload.controller === "human") return "waiting";
  if (last.role === "bot") {
    if (last.payload.kind === "ask_user") return "waiting";
    if (now - Date.parse(last.created_at) < HAPPY_FOR_MS) return "happy";
  }
  if (now - Date.parse(last.created_at) > 6 * HOUR) return "sleeping";
  return "idle";
}
