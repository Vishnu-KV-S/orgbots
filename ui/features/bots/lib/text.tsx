import { Fragment, type ReactNode } from "react";
import type { BotAction } from "@/lib/api/bots";

/**
 * Bot text, rendered without `innerHTML`.
 *
 * A bot's reply carries text it read off web pages, so it is untrusted — rendering
 * it as HTML would let a page put markup into this app. Instead the few things a
 * reply actually uses (links, `code`, **bold**, bullet lines) are tokenised into
 * React nodes, and everything else stays text.
 */

const INLINE = /(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]|`[^`\n]+`|\*\*[^*\n]+\*\*)/g;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const token = match[0];
    const k = `${key}-${i++}`;
    if (token.startsWith("`")) {
      out.push(<code key={k}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      out.push(<strong key={k}>{token.slice(2, -2)}</strong>);
    } else {
      out.push(
        <a key={k} href={token} target="_blank" rel="noopener noreferrer nofollow">
          {token}
        </a>,
      );
    }
    last = at + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function RichText({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];

  const flush = (key: string) => {
    if (bullets.length === 0) return;
    blocks.push(
      <ul key={key}>
        {bullets.map((b, i) => (
          <li key={i}>{inline(b, `${key}-${i}`)}</li>
        ))}
      </ul>,
    );
    bullets = [];
  };

  lines.forEach((line, n) => {
    const bullet = /^\s*(?:[-*•]|\d+\.)\s+(.*)$/.exec(line);
    if (bullet) {
      bullets.push(bullet[1]);
      return;
    }
    flush(`ul-${n}`);
    blocks.push(
      <Fragment key={n}>
        {inline(line, `l-${n}`)}
        {n < lines.length - 1 && "\n"}
      </Fragment>,
    );
  });
  flush("ul-end");
  return <>{blocks}</>;
}

/** One action, the way a person would say it. */
export function describeAction(action: BotAction | undefined): string {
  if (!action) return "";
  const target = action.element_label
    ? `“${action.element_label}”`
    : action.element !== undefined
      ? `[${action.element}]`
      : "";
  switch (action.type) {
    case "navigate":
      return `Open ${action.url ?? ""}`;
    case "click":
      return `Click ${target}`;
    case "type":
      return `Type ${action.secret ? "••••••" : `“${action.text ?? ""}”`} into ${target}${
        action.submit ? " and press Enter" : ""
      }`;
    case "press":
      return `Press ${action.key ?? ""}`;
    case "select":
      return `Choose “${action.option ?? ""}” in ${target}`;
    case "scroll":
      return `Scroll ${action.direction ?? "down"}`;
    case "hover":
      return `Hover over ${target}`;
    case "back":
      return "Go back";
    case "forward":
      return "Go forward";
    case "reload":
      return "Reload the page";
    case "wait":
      return `Wait ${action.seconds ?? 1}s`;
    case "remember":
      return action.bot ? `Teach ${action.bot}` : "Save to memory";
    case "forget":
      return action.bot ? `Remove a memory from ${action.bot}` : "Forget a memory";
    case "recall":
      return `Recall “${action.text ?? ""}”`;
    case "update_brief":
      return action.bot ? `Update ${action.bot}'s brief` : "Update own brief";
    case "create_bot":
      return `Create helper “${action.bot ?? ""}”${action.label ? ` — ${action.label}` : ""}`;
    case "ask_bot":
      return `Ask ${action.bot ?? "helper"}: ${action.text ?? ""}`;
    case "bot_answer":
      return `${action.bot ?? "Helper"} answered`;
    case "observe":
      return "Look at the page";
    case "plan":
      return "Plan";
    case "look":
      return `Look at the screen: “${action.text ?? ""}”`;
    case "sign_in":
      return action.via === "saved"
        ? `Sign in to ${action.host ?? ""} with saved login ${action.label ?? ""}`.trim()
        : `Fill in the ${action.host ?? ""} form with your details`;
    case "list_files":
      return action.text
        ? `Search team files for “${action.text}”`
        : `Look in ${action.path && action.path !== "/" ? action.path : "the team files"}`;
    case "read_file":
      return `Read ${action.path ?? "a file"}`;
    case "write_file":
      return `Write ${action.path ?? "a file"}`;
    case "append_file":
      return `Add to ${action.path ?? "a file"}`;
    case "edit_file":
      return `Edit ${action.path ?? "a file"}`;
    case "move_file":
      return `Move ${action.path ?? "a file"} to ${action.to ?? ""}`;
    case "delete_file":
      return `Delete ${action.path ?? "a file"}`;
    case "save_skill":
      return `Save skill /${action.name ?? ""}`;
    case "use_skill":
      return `Use skill /${action.name ?? ""}`;
    case "save_routine":
      return `Save routine “${action.name ?? ""}”`;
    case "delete_routine":
      return `Delete routine “${action.name ?? ""}”`;
    default:
      return action.type;
  }
}

export const ACTION_ICON: Record<string, string> = {
  navigate: "↗",
  click: "◉",
  type: "⌨",
  press: "⏎",
  select: "☰",
  scroll: "↕",
  hover: "◌",
  back: "←",
  forward: "→",
  reload: "↻",
  wait: "…",
  remember: "✎",
  forget: "⌫",
  recall: "◷",
  update_brief: "≡",
  create_bot: "+",
  ask_bot: "→",
  bot_answer: "←",
  observe: "◎",
  plan: "☑",
  sign_in: "🔑",
  look: "👁",
  list_files: "⌕",
  read_file: "▤",
  write_file: "▦",
  append_file: "⊕",
  edit_file: "✐",
  move_file: "⇢",
  delete_file: "✕",
  save_skill: "✦",
  use_skill: "✦",
  save_routine: "⏰",
  delete_routine: "⏰",
};

/** File steps a person can follow into the Files pane (a deleted file is in the trash). */
export const OPENS_FILE = new Set([
  "read_file",
  "write_file",
  "append_file",
  "edit_file",
  "move_file",
]);

export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}
