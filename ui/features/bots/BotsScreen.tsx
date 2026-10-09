"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type Bot,
  type ComputerStatus,
  computerStatus,
  deleteBot,
  duplicateBot,
  listBots,
  listGroups,
  markRead,
  updateBot,
} from "@/lib/api/bots";
import { ApiUnreachable } from "@/components/ui";
import { useResource } from "@/lib/hooks/useResource";
import { cx } from "@/lib/cx";
import { BotFace, BotStage, type Mood, PRESETS } from "./avatar";
import { Avatar } from "./components/Avatar";
import { ComputerPane } from "./components/ComputerPane";
import { Conversation, type Pane } from "./components/Conversation";
import { DetailsPane } from "./components/DetailsPane";
import { FilesPane } from "./components/FilesPane";
import { GroupConversation, GroupDialog } from "./components/GroupConversation";
import { SkillsPane } from "./components/SkillsPane";
import {
  CommandPalette,
  DeleteBotDialog,
  NewBotDialog,
  SettingsDialog,
  notificationsEnabled,
} from "./components/Dialogs";
import { Sidebar, type BotCommands } from "./components/Sidebar";
import { TEMPLATES } from "./lib/templates";

const LIST_MS = 2500;

const LINEUP_MOODS: Mood[] = ["idle", "browsing", "happy", "thinking", "typing"];

type Dialog = "new" | "palette" | "settings" | "group" | null;

/**
 * The bots workspace: sidebar → conversation → computer / details.
 *
 * Its main interaction is messaging a bot, then inspecting what it did, what it
 * produced and what it is asking for, inside that conversation. Selection lives in
 * the URL (`?bot=`), so a link opens the right conversation and Back works.
 */
export function BotsScreen() {
  const bots = useResource(listBots, { intervalMs: LIST_MS });
  const groups = useResource(listGroups, { intervalMs: LIST_MS });
  // A group chat is selected instead of a bot, never alongside one (`?group=`).
  const [groupId, setGroupId] = useState<string | null>(null);
  const computer = useResource<ComputerStatus>(computerStatus, {
    intervalMs: 5000,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pane, setPane] = useState<Pane>(null);
  // A file a conversation's activity line asked to open; the Files pane clears it.
  const [openFile, setOpenFile] = useState<string | null>(null);
  const openInFiles = useCallback((path: string) => {
    setOpenFile(path);
    setPane("files");
  }, []);
  const fileOpened = useCallback(() => setOpenFile(null), []);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [toast, setToast] = useState<{
    text: string;
    undo?: () => void;
  } | null>(null);
  const [showList, setShowList] = useState(false);
  const [deleting, setDeleting] = useState<Bot | null>(null);

  const list = bots.data?.bots ?? null;
  const selected = useMemo(
    () => (groupId ? null : (list?.find((b) => b.id === selectedId) ?? null)),
    [list, selectedId, groupId],
  );
  const group = useMemo(
    () => groups.data?.groups.find((g) => g.id === groupId) ?? null,
    [groups.data, groupId],
  );

  // --- selection, in the URL ----------------------------------------------------------
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setGroupId(params.get("group"));
    const fromUrl = params.get("bot");
    let remembered: string | null = null;
    try {
      remembered = localStorage.getItem("last-bot");
    } catch {
      /* ignore */
    }
    setSelectedId(fromUrl ?? remembered);
    const onPop = () => {
      const now = new URLSearchParams(window.location.search);
      setGroupId(now.get("group"));
      setSelectedId(now.get("bot"));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const selectGroup = useCallback((id: string | null) => {
    setGroupId(id);
    setShowList(false);
    window.history.pushState(null, "", id ? `/?group=${id}` : "/");
  }, []);

  const select = useCallback((id: string | null) => {
    setSelectedId(id);
    setGroupId(null);
    setShowList(false);
    const url = id ? `/?bot=${id}` : "/";
    window.history.pushState(null, "", url);
    try {
      if (id) localStorage.setItem("last-bot", id);
    } catch {
      /* ignore */
    }
  }, []);

  // A remembered bot that no longer exists falls back to the first one — except a
  // bot created a moment ago, which the list in hand simply predates.
  const justCreated = useRef<string | null>(null);
  useEffect(() => {
    if (!list) return;
    if (justCreated.current && list.some((b) => b.id === justCreated.current)) {
      justCreated.current = null;
    }
    if (selectedId && selectedId === justCreated.current) return;
    if (selectedId && !list.some((b) => b.id === selectedId)) {
      setSelectedId(list.find((b) => !b.hidden)?.id ?? null);
    } else if (!selectedId && list.length > 0) {
      setSelectedId(list.find((b) => !b.hidden)?.id ?? list[0].id);
    }
  }, [list, selectedId]);

  // --- ⌘K -----------------------------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setDialog((d) => (d === "palette" ? null : "palette"));
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // --- notifications: a bot that just became unread, while you are not looking --------
  const previous = useRef<Map<string, boolean>>(new Map());
  useEffect(() => {
    if (!list) return;
    const before = previous.current;
    const unreadCount = list.filter((b) => b.unread || b.needs_attention).length;
    document.title = unreadCount > 0 ? `(${unreadCount}) Bots` : "Bots";
    for (const bot of list) {
      const was = before.get(bot.id);
      const now = bot.unread || bot.needs_attention;
      const looking = bot.id === selectedId && document.visibilityState === "visible";
      if (was === false && now && !looking && notificationsEnabled()) {
        if ("Notification" in window && Notification.permission === "granted") {
          const n = new Notification(bot.name, {
            body: bot.needs_attention ? "Needs your attention" : (bot.last_message?.content ?? ""),
            tag: bot.id,
          });
          n.onclick = () => {
            window.focus();
            select(bot.id);
          };
        }
      }
      before.set(bot.id, now);
    }
  }, [list, selectedId, select]);

  // --- commands -----------------------------------------------------------------------
  const refresh = bots.refresh;
  const commands: BotCommands = useMemo(
    () => ({
      edit: (bot) => {
        select(bot.id);
        setPane("details");
      },
      duplicate: async (bot) => {
        const copy = await duplicateBot(bot.id);
        refresh();
        select(copy.id);
        setToast({
          text: `Duplicated ${bot.name}. Memory and history were not copied.`,
        });
      },
      togglePin: async (bot) => {
        await updateBot(bot.id, { pinned: !bot.pinned });
        refresh();
      },
      toggleHidden: async (bot) => {
        await updateBot(bot.id, { hidden: !bot.hidden });
        refresh();
        setToast({
          text: bot.hidden
            ? `${bot.name} is visible again.`
            : `${bot.name} is hidden. Its work continues.`,
          undo: async () => {
            await updateBot(bot.id, { hidden: bot.hidden });
            refresh();
          },
        });
      },
      toggleRead: async (bot) => {
        await markRead(bot.id, !bot.unread);
        refresh();
      },
      remove: (bot) => setDeleting(bot),
    }),
    [refresh, select, selectedId],
  );

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  const created = (bot: Bot) => {
    justCreated.current = bot.id;
    setDialog(null);
    refresh();
    select(bot.id);
  };

  return (
    <BotStage>
      <div className={cx("bots", selected && pane && "with-pane", showList && "show-list")}>
        <Sidebar
          bots={list}
          selectedId={selectedId}
          onSelect={select}
          onNew={() => setDialog("new")}
          onSearch={() => setDialog("palette")}
          onSettings={() => setDialog("settings")}
          commands={commands}
          computer={computer.data}
          error={bots.error}
          groups={groups.data?.groups ?? null}
          selectedGroupId={groupId}
          onSelectGroup={selectGroup}
          onNewGroup={() => setDialog("group")}
        />

        {group ? (
          <GroupConversation
            key={group.id}
            group={group}
            bots={list ?? []}
            onChanged={groups.refresh}
            onDeleted={() => {
              selectGroup(null);
              groups.refresh();
            }}
            onOpenBot={select}
          />
        ) : selected ? (
          <Conversation
            key={selected.id}
            bot={selected}
            bots={list ?? []}
            pane={pane}
            onPane={setPane}
            onBack={() => setShowList(true)}
            onChanged={refresh}
            onOpenFile={openInFiles}
          />
        ) : (
          <section className="convo">
            {bots.error && !list ? (
              <div className="welcome">
                <ApiUnreachable detail={bots.error} />
              </div>
            ) : (
              <div className="welcome">
                <div className="lineup" aria-hidden>
                  {PRESETS.map((p, i) => (
                    <BotFace
                      key={p.name}
                      appearance={p.appearance}
                      mood={LINEUP_MOODS[i % LINEUP_MOODS.length]}
                      size={130}
                      detail="high"
                      seed={p.name}
                    />
                  ))}
                </div>
                <h1>Your AI employees</h1>
                <p>
                  Create bots for different jobs. Each one has its own browser screen on a shared
                  cloud computer, remembers your preferences, shows every step it takes, and asks
                  before anything consequential.
                </p>
                <div className="templates">
                  {TEMPLATES.slice(0, 4).map((t) => (
                    <button
                      key={t.name}
                      type="button"
                      className="template"
                      onClick={() => setDialog("new")}
                    >
                      <Avatar appearance={t.appearance} size={34} />
                      <strong>{t.name}</strong>
                      <span>{t.blurb}</span>
                    </button>
                  ))}
                </div>
                <button type="button" className="pbtn primary" onClick={() => setDialog("new")}>
                  + Create a bot
                </button>
              </div>
            )}
          </section>
        )}

        {selected && pane && (
          <aside
            className="bpane"
            aria-label={
              pane === "computer"
                ? "Computer"
                : pane === "files"
                  ? "Team files"
                  : pane === "skills"
                    ? "Skills"
                    : "Details"
            }
          >
            <div className="bpane-tabs">
              <button
                type="button"
                className={cx("tab", pane === "computer" && "on")}
                onClick={() => setPane("computer")}
              >
                Computer
              </button>
              <button
                type="button"
                className={cx("tab", pane === "files" && "on")}
                onClick={() => setPane("files")}
              >
                Files
              </button>
              <button
                type="button"
                className={cx("tab", pane === "skills" && "on")}
                onClick={() => setPane("skills")}
              >
                Skills
              </button>
              <button
                type="button"
                className={cx("tab", pane === "details" && "on")}
                onClick={() => setPane("details")}
              >
                Details
              </button>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="ibtn"
                onClick={() => setPane(null)}
                aria-label="Close pane"
              >
                ✕
              </button>
            </div>
            <div className="bpane-body">
              {pane === "computer" ? (
                <ComputerPane key={selected.id} bot={selected} />
              ) : pane === "skills" ? (
                <SkillsPane />
              ) : pane === "files" ? (
                <FilesPane
                  key={selected.team_id}
                  bot={selected}
                  openPath={openFile}
                  onOpened={fileOpened}
                />
              ) : (
                <DetailsPane
                  key={selected.id}
                  bot={selected}
                  bots={list ?? []}
                  onSelect={select}
                  onChanged={refresh}
                  onDuplicate={() => void commands.duplicate(selected)}
                  onDelete={() => void commands.remove(selected)}
                />
              )}
            </div>
          </aside>
        )}

        {deleting && (
          <DeleteBotDialog
            bot={deleting}
            bots={list ?? []}
            onClose={() => setDeleting(null)}
            onDelete={async (withHelpers) => {
              const result = await deleteBot(deleting.id, withHelpers);
              if (selectedId && result.deleted.includes(selectedId)) select(null);
              setDeleting(null);
              refresh();
              const extra = result.deleted.length - 1;
              setToast({
                text:
                  extra > 0
                    ? `Deleted ${deleting.name} and ${extra} helper${extra === 1 ? "" : "s"}.`
                    : `Deleted ${deleting.name}.`,
              });
            }}
          />
        )}
        {dialog === "new" && <NewBotDialog onClose={() => setDialog(null)} onCreated={created} />}
        {dialog === "group" && (
          <GroupDialog
            bots={list ?? []}
            onClose={() => setDialog(null)}
            onSaved={(g) => {
              setDialog(null);
              groups.refresh();
              selectGroup(g.id);
            }}
          />
        )}
        {dialog === "palette" && (
          <CommandPalette
            bots={list ?? []}
            onClose={() => setDialog(null)}
            onSelectBot={select}
            onNew={() => setDialog("new")}
            onSettings={() => setDialog("settings")}
          />
        )}
        {dialog === "settings" && (
          <SettingsDialog onClose={() => setDialog(null)} computer={computer.data} />
        )}

        {toast && (
          <div className="toast" role="status">
            {toast.text}
            {toast.undo && (
              <button
                type="button"
                onClick={() => {
                  void toast.undo?.();
                  setToast(null);
                }}
              >
                Undo
              </button>
            )}
          </div>
        )}
      </div>
    </BotStage>
  );
}
