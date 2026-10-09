import type { Ionicons } from "@expo/vector-icons";
import type { BotAction } from "./api";

/** One action, the way a person would say it. Mirrors `ui/features/bots/lib/text.tsx`. */
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
    case "upload":
      return `Upload ${(action.paths ?? []).map((p) => p.split("/").pop()).join(", ") || "a file"}`;
    case "remember":
      return action.bot ? `Teach ${action.bot}` : "Save to memory";
    case "forget":
      return action.bot ? `Remove a memory from ${action.bot}` : "Forget a memory";
    case "recall":
      return `Recall “${action.text ?? ""}”`;
    case "update_brief":
      return action.bot ? `Update ${action.bot}'s brief` : "Update own brief";
    case "create_bot":
      return `Create helper “${action.bot ?? ""}”`;
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
      return `Sign in to ${action.host ?? "the site"}`;
    case "list_files":
      return action.text ? `Search team files for “${action.text}”` : "Look in the team files";
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
    case "copy_file":
      return `Copy ${action.path ?? "a file"} to ${action.to ?? ""}`;
    case "use_connector":
      return `Use ${action.text ?? "a connected app"}`;
    case "review":
      return `Auto Review: ${
        action.verdict === "allow" ? "allowed" : action.verdict === "deny" ? "refused" : "asked you"
      } — ${action.text ?? ""}`;
    case "run_command":
      return `Run a command: ${action.text ?? ""}`;
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

type IconName = keyof typeof Ionicons.glyphMap;

export const ACTION_ICON: Record<string, IconName> = {
  navigate: "globe-outline",
  click: "finger-print-outline",
  type: "create-outline",
  press: "return-down-back-outline",
  select: "chevron-expand-outline",
  scroll: "swap-vertical-outline",
  hover: "locate-outline",
  back: "arrow-back-outline",
  forward: "arrow-forward-outline",
  reload: "refresh-outline",
  wait: "pause-circle-outline",
  upload: "cloud-upload-outline",
  remember: "bookmark-outline",
  forget: "bookmark-outline",
  recall: "book-outline",
  update_brief: "clipboard-outline",
  create_bot: "person-add-outline",
  ask_bot: "chatbubbles-outline",
  bot_answer: "chatbubble-ellipses-outline",
  observe: "scan-outline",
  plan: "list-outline",
  sign_in: "key-outline",
  look: "eye-outline",
  list_files: "folder-open-outline",
  read_file: "document-text-outline",
  write_file: "document-outline",
  append_file: "document-attach-outline",
  edit_file: "pencil-outline",
  move_file: "folder-outline",
  delete_file: "trash-outline",
  copy_file: "copy-outline",
  review: "shield-checkmark-outline",
  use_connector: "apps-outline",
  run_command: "terminal-outline",
  save_skill: "sparkles-outline",
  use_skill: "sparkles-outline",
  save_routine: "time-outline",
  delete_routine: "time-outline",
};

export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

/** A one-line preview of a message for the bots list. */
export function preview(content: string): string {
  return content
    .replace(/\*\*|`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Inline tokens a reply actually uses: links, `code` and **bold**. */
export const INLINE = /(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]|`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
