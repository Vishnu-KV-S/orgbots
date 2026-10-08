"use client";

import Link from "next/link";
import { useState } from "react";
import type { Bot, ComputerStatus } from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { timeAgo } from "../lib/text";
import { Avatar } from "./Avatar";
import { Menu, type MenuItem } from "./Menu";

export interface BotCommands {
  edit: (bot: Bot) => void;
  duplicate: (bot: Bot) => void;
  togglePin: (bot: Bot) => void;
  toggleHidden: (bot: Bot) => void;
  toggleRead: (bot: Bot) => void;
  remove: (bot: Bot) => void;
}

function preview(bot: Bot): string {
  if (bot.working) return "Working…";
  if (bot.needs_attention) return "Needs your attention";
  const last = bot.last_message;
  if (!last) return bot.label || bot.description || "No messages yet";
  if (last.role === "approval") return "Waiting for approval";
  const text = last.content.replace(/\*\*|`/g, "").replace(/\s+/g, " ");
  return (last.role === "user" ? "You: " : "") + text;
}

function BotRow({
  bot,
  active,
  onSelect,
  commands,
}: {
  bot: Bot;
  active: boolean;
  onSelect: () => void;
  commands: BotCommands;
}) {
  const items: MenuItem[] = [
    { label: "Edit profile", onSelect: () => commands.edit(bot) },
    { label: "Duplicate", onSelect: () => commands.duplicate(bot) },
    { label: bot.pinned ? "Unpin" : "Pin", onSelect: () => commands.togglePin(bot) },
    { label: bot.hidden ? "Unhide" : "Hide", onSelect: () => commands.toggleHidden(bot) },
    {
      label: bot.unread ? "Mark as read" : "Mark as unread",
      onSelect: () => commands.toggleRead(bot),
    },
    { label: "Delete…", onSelect: () => commands.remove(bot), danger: true },
  ];
  return (
    <div
      role="button"
      tabIndex={0}
      className={cx("brow", active && "active", bot.unread && "unread")}
      onClick={onSelect}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect()}
    >
      <Avatar name={bot.name} avatar={bot.avatar} working={bot.working} />
      <div className="brow-main">
        <div className="brow-name">
          {bot.name}
          {bot.pinned && <span className="pin" title="Pinned">●</span>}
        </div>
        <div className="brow-sub">{preview(bot)}</div>
      </div>
      <div className="brow-marks">
        {bot.needs_attention ? (
          <span className="mark-attention" title="Needs attention">
            !
          </span>
        ) : bot.unread ? (
          <span className="mark-unread" title="Unread" />
        ) : bot.last_message ? (
          <span className="brow-sub">{timeAgo(bot.last_message.created_at)}</span>
        ) : null}
        <Menu items={items} label={`${bot.name} menu`} />
      </div>
    </div>
  );
}

export function Sidebar({
  bots,
  selectedId,
  onSelect,
  onNew,
  onSearch,
  onSettings,
  commands,
  computer,
  error,
}: {
  bots: Bot[] | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onSearch: () => void;
  onSettings: () => void;
  commands: BotCommands;
  computer: ComputerStatus | null;
  error: string | null;
}) {
  const [showHidden, setShowHidden] = useState(false);
  const visible = (bots ?? []).filter((b) => showHidden || !b.hidden || b.id === selectedId);
  const pinned = visible.filter((b) => b.pinned);
  const rest = visible.filter((b) => !b.pinned);
  const hiddenCount = (bots ?? []).filter((b) => b.hidden).length;

  const row = (bot: Bot) => (
    <BotRow
      key={bot.id}
      bot={bot}
      active={bot.id === selectedId}
      onSelect={() => onSelect(bot.id)}
      commands={commands}
    />
  );

  return (
    <aside className="bside">
      <div className="bside-head">
        <div className="bside-title">
          <span className="dot" />
          Bots
        </div>
        <span style={{ flex: 1 }} />
        <button type="button" className="pbtn" onClick={onNew} title="Create a new bot">
          + New
        </button>
      </div>
      <button type="button" className="bside-search" onClick={onSearch}>
        Search bots and messages
        <kbd>⌘K</kbd>
      </button>

      <nav className="bside-list" aria-label="Bots">
        {error && !bots && <p className="brow-sub" style={{ padding: 8 }}>API unreachable</p>}
        {bots && bots.length === 0 && (
          <p className="brow-sub" style={{ padding: 8, whiteSpace: "normal" }}>
            No bots yet. Create one to get started.
          </p>
        )}
        {pinned.length > 0 && <div className="bside-group">Pinned</div>}
        {pinned.map(row)}
        {pinned.length > 0 && rest.length > 0 && <div className="bside-group">Bots</div>}
        {rest.map(row)}
        {hiddenCount > 0 && (
          <button
            type="button"
            className="bside-link"
            style={{ marginTop: 6 }}
            onClick={() => setShowHidden((s) => !s)}
          >
            {showHidden ? "Hide hidden bots" : `Show ${hiddenCount} hidden`}
          </button>
        )}
      </nav>

      <div className="bside-foot">
        <button type="button" className="bside-link" onClick={onSettings}>
          ⚙ Settings
          <span
            className={cx(
              "status-dot",
              computer?.reachable ? "ok" : computer ? "bad" : undefined,
            )}
            title={
              computer?.reachable
                ? "Cloud computer is running"
                : "Cloud computer is not reachable"
            }
          />
        </button>
        <Link href="/companies" className="bside-link">
          ▦ Companies console
        </Link>
      </div>
    </aside>
  );
}
