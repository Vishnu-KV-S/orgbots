"use client";

import Link from "next/link";
import { useState } from "react";
import type { Bot, ComputerStatus } from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { timeAgo } from "../lib/text";
import { moodOfBot } from "../avatar";
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
  depth = 0,
  helperCount = 0,
  collapsed = false,
  onToggle,
}: {
  bot: Bot;
  active: boolean;
  onSelect: () => void;
  commands: BotCommands;
  depth?: number;
  helperCount?: number;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const items: MenuItem[] = [
    { label: "Edit profile", onSelect: () => commands.edit(bot) },
    { label: "Duplicate", onSelect: () => commands.duplicate(bot) },
    {
      label: bot.pinned ? "Unpin" : "Pin",
      onSelect: () => commands.togglePin(bot),
    },
    {
      label: bot.hidden ? "Unhide" : "Hide",
      onSelect: () => commands.toggleHidden(bot),
    },
    {
      label: bot.unread ? "Mark as read" : "Mark as unread",
      onSelect: () => commands.toggleRead(bot),
    },
    { label: "Delete…", onSelect: () => commands.remove(bot), danger: true },
  ];
  return (
    // Not `role="button"` on the row: that would make the twisty and the menu inside
    // it presentational, unreachable by keyboard and by a screen reader. The row is a
    // plain container; selecting is its own button, and the click anywhere else on the
    // row is a pointer convenience.
    <div
      className={cx("brow", active && "active", bot.unread && "unread", depth > 0 && "helper")}
      style={depth > 0 ? { paddingLeft: 8 + depth * 18 } : undefined}
      onClick={onSelect}
    >
      {helperCount > 0 ? (
        <button
          type="button"
          className="twisty"
          aria-label={collapsed ? `Show ${bot.name}'s helpers` : `Hide ${bot.name}'s helpers`}
          aria-expanded={!collapsed}
          onClick={(e) => {
            e.stopPropagation();
            onToggle?.();
          }}
        >
          {collapsed ? "▸" : "▾"}
        </button>
      ) : depth > 0 ? (
        <span className="twisty-space" aria-hidden>
          ↳
        </span>
      ) : null}
      <button
        type="button"
        className="brow-hit"
        aria-current={active ? "true" : undefined}
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
      >
        <Avatar bot={bot} mood={moodOfBot(bot)} size={depth > 0 ? 30 : 38} live />
        <div className="brow-main">
          <div className="brow-name">
            {bot.name}
            {bot.pinned && (
              <span className="pin" title="Pinned">
                ●
              </span>
            )}
          </div>
          <div className="brow-sub">
            {helperCount > 0 && collapsed
              ? `${helperCount} helper${helperCount === 1 ? "" : "s"} · `
              : ""}
            {preview(bot)}
          </div>
        </div>
      </button>
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
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const visible = (bots ?? []).filter((b) => showHidden || !b.hidden || b.id === selectedId);
  const ids = new Set(visible.map((b) => b.id));
  // A helper is listed under its parent; one whose parent is not visible (hidden, or
  // deleted with "keep helpers") is listed at the top level.
  const childrenOf = new Map<string, Bot[]>();
  for (const b of visible) {
    if (b.parent_bot_id && ids.has(b.parent_bot_id)) {
      childrenOf.set(b.parent_bot_id, [...(childrenOf.get(b.parent_bot_id) ?? []), b]);
    }
  }
  const roots = visible.filter((b) => !b.parent_bot_id || !ids.has(b.parent_bot_id));
  const pinned = roots.filter((b) => b.pinned);
  const rest = roots.filter((b) => !b.pinned);
  const hiddenCount = (bots ?? []).filter((b) => b.hidden).length;

  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const row = (bot: Bot, depth = 0): React.ReactNode => {
    const kids = childrenOf.get(bot.id) ?? [];
    const isCollapsed = collapsed.has(bot.id);
    return (
      <div key={bot.id}>
        <BotRow
          bot={bot}
          active={bot.id === selectedId}
          onSelect={() => onSelect(bot.id)}
          commands={commands}
          depth={depth}
          helperCount={kids.length}
          collapsed={isCollapsed}
          onToggle={() => toggle(bot.id)}
        />
        {!isCollapsed && kids.map((kid) => row(kid, depth + 1))}
      </div>
    );
  };

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
        {error && !bots && (
          <p className="brow-sub" style={{ padding: 8 }}>
            API unreachable
          </p>
        )}
        {bots && bots.length === 0 && (
          <p className="brow-sub" style={{ padding: 8, whiteSpace: "normal" }}>
            No bots yet. Create one to get started.
          </p>
        )}
        {pinned.length > 0 && <div className="bside-group">Pinned</div>}
        {pinned.map((b) => row(b))}
        {pinned.length > 0 && rest.length > 0 && <div className="bside-group">Bots</div>}
        {rest.map((b) => row(b))}
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
            className={cx("status-dot", computer?.reachable ? "ok" : computer ? "bad" : undefined)}
            title={
              computer?.reachable ? "Cloud computer is running" : "Cloud computer is not reachable"
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
