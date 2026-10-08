import { type BotDraft, EMPTY_BRIEF } from "@/lib/api/bots";
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
    brief: {
      ...EMPTY_BRIEF,
      mission: "Answer my research questions with well-sourced, current information.",
      duties: [
        "Find and read primary sources",
        "Compare options with their trade-offs",
        "Report back with links for every claim",
      ],
      style: "Short, table-like lists. Lead with the answer.",
      escalation: "Ask me when sources disagree on something that matters.",
    },
    blurb: "Find and compare information, with sources",
    appearance: look("Orbit"),
  },
  {
    avatar: "🛒",
    name: "Shopper",
    label: "Price hunter",
    description: "Searches stores for products, compares prices and availability.",
    brief: {
      ...EMPTY_BRIEF,
      mission: "Find the best price for what I want to buy.",
      duties: [
        "Search several stores",
        "Check availability and delivery",
        "Report the store, price and link",
      ],
      boundaries: ["Never place an order or enter payment details without asking me first"],
    },
    blurb: "Compare products and prices across stores",
    appearance: look("Cubey"),
  },
  {
    avatar: "✉️",
    name: "Inbox assistant",
    label: "Email & messages",
    description: "Reads and drafts messages in web mail once you have signed in for it.",
    brief: {
      ...EMPTY_BRIEF,
      mission: "Keep my inbox under control.",
      duties: ["Triage new mail by urgency", "Draft replies to what needs one"],
      boundaries: ["Never send anything without my approval"],
      style: "Drafts are short and in my voice.",
    },
    blurb: "Triage and draft replies in web mail",
    appearance: look("Beacon"),
  },
  {
    avatar: "📣",
    name: "Social manager",
    label: "Social media",
    description: "Monitors mentions and drafts posts for your social accounts.",
    brief: {
      ...EMPTY_BRIEF,
      mission: "Look after my social media presence.",
      duties: ["Watch mentions and replies", "Draft posts for my approval"],
      boundaries: ["Never post publicly without asking me first"],
    },
    blurb: "Watch mentions and draft posts",
    appearance: look("Telly"),
  },
  {
    avatar: "🤖",
    name: "Assistant",
    label: "",
    description: "",
    brief: EMPTY_BRIEF,
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
