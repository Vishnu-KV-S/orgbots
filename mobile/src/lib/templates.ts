import { type Appearance, type BotDraft, EMPTY_BRIEF } from "./api";
import { PRESETS } from "./appearance";

/** Starting points for a new bot, the same as the web app's. Editable after creation. */
export const TEMPLATES: (BotDraft & { blurb: string; appearance: Appearance })[] = [
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
    appearance: PRESETS.Orbit,
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
    appearance: PRESETS.Cubey,
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
    appearance: PRESETS.Beacon,
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
    appearance: PRESETS.Telly,
  },
  {
    avatar: "🤖",
    name: "Assistant",
    label: "",
    description: "",
    brief: EMPTY_BRIEF,
    blurb: "A blank bot you shape yourself",
    appearance: PRESETS.Sprout,
  },
];

/** Starters on an empty conversation, in the spirit of the web app's `/` prompts. */
export const STARTERS: { title: string; text: string }[] = [
  {
    title: "Research a topic",
    text: "Research the following and report the 5 most useful sources, with links: ",
  },
  {
    title: "Compare options",
    text: "Compare these options and recommend one, with the trade-offs: ",
  },
  {
    title: "Summarize a page",
    text: "Open this page and summarize the key points in a short list: ",
  },
  { title: "Progress report", text: "Where are you up to? Give me a short progress report." },
];
