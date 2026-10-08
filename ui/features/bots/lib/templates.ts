import type { BotDraft } from "@/lib/api/bots";
import { type Appearance, PRESETS } from "../avatar/appearance";

const look = (name: string): Appearance =>
  (PRESETS.find((p) => p.name === name) ?? PRESETS[0]).appearance;

/** Starting points for a new bot. Everything here is editable after creation. */
export const TEMPLATES: (BotDraft & {
  blurb: string;
  appearance: Appearance;
})[] = [
  {
    avatar: "🔎",
    name: "Researcher",
    label: "Web research",
    description: "Finds, reads and compares sources on the web, and reports back with links.",
    instructions:
      "Prefer primary sources. Always include links. When comparing options, give a short table-like list with the trade-offs.",
    blurb: "Find and compare information, with sources",
    appearance: look("Orbit"),
  },
  {
    avatar: "🛒",
    name: "Shopper",
    label: "Price hunter",
    description: "Searches stores for products, compares prices and availability.",
    instructions:
      "Never place an order or enter payment details without asking me first. Report prices with the store name and link.",
    blurb: "Compare products and prices across stores",
    appearance: look("Cubey"),
  },
  {
    avatar: "✉️",
    name: "Inbox assistant",
    label: "Email & messages",
    description: "Reads and drafts messages in web mail once you have signed in for it.",
    instructions:
      "Draft replies but never send anything without my approval. Keep drafts short and in my voice.",
    blurb: "Triage and draft replies in web mail",
    appearance: look("Beacon"),
  },
  {
    avatar: "📣",
    name: "Social manager",
    label: "Social media",
    description: "Monitors mentions and drafts posts for your social accounts.",
    instructions: "Never post publicly without asking me first.",
    blurb: "Watch mentions and draft posts",
    appearance: look("Telly"),
  },
  {
    avatar: "🤖",
    name: "Assistant",
    label: "",
    description: "",
    instructions: "",
    blurb: "A blank bot you configure yourself",
    appearance: look("Sprout"),
  },
];

/** The `/` menu. Built-in prompts until saved skills land. */
export const QUICK_PROMPTS: { cmd: string; text: string; hint: string }[] = [
  {
    cmd: "continue",
    text: "Continue where you left off.",
    hint: "Resume the last task",
  },
  {
    cmd: "summarize",
    text: "Summarize the page you have open: the key points, in a short list.",
    hint: "Summarize the current page",
  },
  {
    cmd: "research",
    text: "Research the following and report the 5 most useful sources, one line each, with links: ",
    hint: "Research a topic",
  },
  {
    cmd: "compare",
    text: "Compare these options and recommend one, with the trade-offs: ",
    hint: "Compare options",
  },
  {
    cmd: "remember",
    text: "Remember this for future work: ",
    hint: "Save a preference to memory",
  },
  {
    cmd: "status",
    text: "Where are you up to? Give me a short progress report.",
    hint: "Ask for a progress report",
  },
];
