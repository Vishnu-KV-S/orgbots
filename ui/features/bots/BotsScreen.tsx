"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type Bot,
  type ComputerStatus,
  computerStatus,
  deleteBot,
  duplicateBot,
  listBots,
  markRead,
  updateBot,
} from "@/lib/api/bots";
import { ApiUnreachable } from "@/components/ui";
import { useResource } from "@/lib/hooks/useResource";
import { cx } from "@/lib/cx";
import { Avatar } from "./components/Avatar";
import { ComputerPane } from "./components/ComputerPane";
import { Conversation, type Pane } from "./components/Conversation";
import { DetailsPane } from "./components/DetailsPane";
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

type Dialog = "new" | "palette" | "settings" | null;

/**
 * The bots workspace: sidebar → conversation → computer / details.
 *
 * Its main interaction is messaging a bot, then inspecting what it did, what it
 * produced and what it is asking for, inside that conversation. Selection lives in
 * the URL (`?bot=`), so a link opens the right conversation and Back works.
 */
export function BotsScreen() {
  const bots = useResource(listBots, { intervalMs: LIST_MS });
  const computer = useResource<ComputerStatus>(computerStatus, { intervalMs: 5000 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pane, setPane] = useState<Pane>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [toast, setToast] = useState<{ text: string; undo?: () => void } | null>(null);
  const [showList, setShowList] = useState(false);
  const [deleting, setDeleting] = useState<Bot | null>(null);

  const list = bots.data?.bots ?? null;
  const selected = useMemo(
    () => list?.find((b) => b.id === selectedId) ?? null,
    [list, selectedId],
  );

  // --- selection, in the URL ----------------------------------------------------------
  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("bot");
    let remembered: string | null = null;
    try {
      remembered = localStorage.getItem("last-bot");
    } catch {
      /* ignore */
    }
    setSelectedId(fromUrl ?? remembered);
    const onPop = () => setSelectedId(new URLSearchParams(window.location.search).get("bot"));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const select = useCallback((id: string | null) => {
    setSelectedId(id);
    setShowList(false);
    const url = id ? `/?bot=${id}` : "/";
    window.history.pushState(null, "", url);
    try {
      if (id) localStorage.setItem("last-bot", id);
    } catch {
      /* ignore */
    }
  }, []);

  // A remembered bot that no longer exists falls back to the first one.
  useEffect(() => {
    if (!list) return;
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
            body: bot.needs_attention ? "Needs your attention" : bot.last_message?.content ?? "",
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
        setToast({ text: `Duplicated ${bot.name}. Memory and history were not copied.` });
      },
      togglePin: async (bot) => {
        await updateBot(bot.id, { pinned: !bot.pinned });
        refresh();
      },
      toggleHidden: async (bot) => {
        await updateBot(bot.id, { hidden: !bot.hidden });
        refresh();
        setToast({
          text: bot.hidden ? `${bot.name} is visible again.` : `${bot.name} is hidden. Its work continues.`,
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
    setDialog(null);
    refresh();
    select(bot.id);
  };

  return (
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
      />

      {selected ? (
        <Conversation
          key={selected.id}
          bot={selected}
          bots={list ?? []}
          pane={pane}
          onPane={setPane}
          onBack={() => setShowList(true)}
          onChanged={refresh}
        />
      ) : (
        <section className="convo">
          {bots.error && !list ? (
            <div className="welcome">
              <ApiUnreachable detail={bots.error} />
            </div>
          ) : (
            <div className="welcome">
              <h1>Your AI employees</h1>
              <p>
                Create bots for different jobs. Each one has its own browser screen on a shared
                cloud computer, remembers your preferences, shows every step it takes, and asks
                before anything consequential.
              </p>
              <div className="templates">
                {TEMPLATES.slice(0, 4).map((t) => (
                  <button key={t.name} type="button" className="template" onClick={() => setDialog("new")}>
                    <Avatar name={t.name} avatar={t.avatar} size="sm" />
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
        <aside className="bpane" aria-label={pane === "computer" ? "Computer" : "Details"}>
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
              className={cx("tab", pane === "details" && "on")}
              onClick={() => setPane("details")}
            >
              Details
            </button>
            <span style={{ flex: 1 }} />
            <button type="button" className="ibtn" onClick={() => setPane(null)} aria-label="Close pane">
              ✕
            </button>
          </div>
          <div className="bpane-body">
            {pane === "computer" ? (
              <ComputerPane key={selected.id} bot={selected} />
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
  );
}
