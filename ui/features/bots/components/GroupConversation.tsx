"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type Group,
  type GroupMessage,
  REACTIONS,
  createGroup,
  deleteGroup,
  groupMessages,
  markGroupRead,
  postToGroup,
  reactInGroup,
  updateGroup,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useAction } from "@/lib/hooks/useAction";
import { RichText, timeAgo } from "../lib/text";
import { Avatar } from "./Avatar";
import { Modal } from "./Dialogs";

const FAST_MS = 1200;
const SLOW_MS = 3000;

/** The group's transcript, polled with a cursor like a bot's (`useConversation`). */
function useGroupMessages(groupId: string) {
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [working, setWorking] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const current = useRef(groupId);

  const tick = useCallback(async () => {
    const id = current.current;
    let next = SLOW_MS;
    try {
      const page = await groupMessages(id, cursor.current);
      if (current.current !== id) return;
      if (page.messages.length) {
        cursor.current = page.messages[page.messages.length - 1].seq;
        setMessages((prev) => {
          const seen = new Set(prev.map((m) => m.id));
          return [...prev, ...page.messages.filter((m) => !seen.has(m.id))];
        });
      }
      setWorking(page.working);
      setError(null);
      next = page.working.length ? FAST_MS : SLOW_MS;
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      if (current.current === id) {
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void tick(), next);
      }
    }
  }, []);

  useEffect(() => {
    current.current = groupId;
    cursor.current = 0;
    setMessages([]);
    setWorking([]);
    void tick();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [groupId, tick]);

  const poke = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    void tick();
  }, [tick]);

  return { messages, working, error, poke };
}

/**
 * A group chat: the person and two to six bots. `@Name` gives a bot the request,
 * `@everyone` gives it to all of them, and a message naming nobody goes to the lead.
 * A bot hands a part to a teammate by naming them in its answer. Reply in a thread to
 * keep feedback on one result together.
 */
export function GroupConversation({
  group,
  bots,
  onChanged,
  onDeleted,
  onOpenBot,
}: {
  group: Group;
  bots: Bot[];
  onChanged: () => void;
  onDeleted: () => void;
  onOpenBot: (id: string) => void;
}) {
  const convo = useGroupMessages(group.id);
  const [thread, setThread] = useState<GroupMessage | null>(null);
  const [editing, setEditing] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, string[]>>({});
  const scroller = useRef<HTMLDivElement>(null);
  const byId = useMemo(() => new Map(bots.map((b) => [b.id, b])), [bots]);

  useEffect(() => {
    if (group.unread) void markGroupRead(group.id).then(onChanged, () => undefined);
  }, [group.id, group.unread, convo.messages.length, onChanged]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [convo.messages.length, thread]);

  const main = convo.messages.filter((m) => !m.thread_root);
  const replies = (root: string) => convo.messages.filter((m) => m.thread_root === root);
  const reactionsOf = (m: GroupMessage) => overrides[m.id] ?? m.reactions;
  const react = async (m: GroupMessage, emoji: string) => {
    const on = !reactionsOf(m).includes(emoji);
    try {
      const r = await reactInGroup(group.id, m.id, emoji, on);
      setOverrides((o) => ({ ...o, [m.id]: r.reactions }));
    } catch {
      /* not saved, not shown */
    }
  };

  const row = (m: GroupMessage, inThread = false) => {
    if (m.author_kind === "system") {
      return (
        <div key={m.id} className="sysline">
          {m.content}
        </div>
      );
    }
    const mine = m.author_kind === "person";
    const author = m.author_bot_id ? byId.get(m.author_bot_id) : undefined;
    const count = inThread ? 0 : replies(m.id).length;
    return (
      <div key={m.id} className={mine ? "msg user" : "msg bot"}>
        {!mine && (author ? <Avatar bot={author} size={24} /> : <span className="gm-dot" />)}
        <div style={{ minWidth: 0, maxWidth: mine ? "82%" : "100%" }}>
          {!mine && (
            <div className="gm-author">
              {author ? (
                <button type="button" className="linklike" onClick={() => onOpenBot(author.id)}>
                  {m.author_name}
                </button>
              ) : (
                m.author_name
              )}
              {" · "}
              {timeAgo(m.created_at)}
            </div>
          )}
          <div className="bubble">{mine ? m.content : <RichText text={m.content} />}</div>
          {reactionsOf(m).length > 0 && (
            <div className="reactions" style={{ justifyContent: mine ? "flex-end" : "start" }}>
              {reactionsOf(m).map((e) => (
                <span key={e} className="reaction">
                  {e}
                </span>
              ))}
            </div>
          )}
          {count > 0 && (
            <button type="button" className="linklike thread-link" onClick={() => setThread(m)}>
              {count} {count === 1 ? "reply" : "replies"} in thread
            </button>
          )}
        </div>
        <div className="msg-actions">
          {!inThread && (
            <button type="button" onClick={() => setThread(m)}>
              Reply in thread
            </button>
          )}
          {REACTIONS.slice(0, 4).map((e) => (
            <button
              key={e}
              type="button"
              onClick={() => void react(m, e)}
              aria-label={`React ${e}`}
            >
              {e}
            </button>
          ))}
        </div>
      </div>
    );
  };

  return (
    <section className="convo group-convo" aria-label={`Group ${group.name}`}>
      <header className="convo-head">
        <div className="group-faces">
          {group.members.slice(0, 4).map((m) => {
            const bot = byId.get(m.id);
            return bot ? <Avatar key={m.id} bot={bot} size={30} /> : null;
          })}
        </div>
        <div className="convo-title">
          <h2>{group.name}</h2>
          <p>
            {group.members.map((m, i) => (
              <span key={m.id}>
                {i > 0 && ", "}
                {m.name}
                {m.id === group.lead_bot_id && " (lead)"}
                {convo.working.includes(m.id) && " · working"}
              </span>
            ))}
          </p>
        </div>
        <button type="button" className="tab" onClick={() => setEditing(true)}>
          ✎ <span className="tab-label">Edit group</span>
        </button>
      </header>

      <div className="convo-scroll" ref={scroller}>
        <div className="convo-inner">
          {main.length === 0 && (
            <div className="welcome" style={{ paddingTop: 40 }}>
              <h1>{group.name}</h1>
              <p>
                Ask the whole group, or name who owns it: <code>@{group.members[0]?.name}</code>,{" "}
                <code>@everyone</code>. A message that names nobody goes to the lead.
              </p>
            </div>
          )}
          {thread ? (
            <div className="thread">
              <button type="button" className="linklike" onClick={() => setThread(null)}>
                ← Back to the group
              </button>
              {row(thread, true)}
              <div className="thread-replies">{replies(thread.id).map((m) => row(m, true))}</div>
            </div>
          ) : (
            main.map((m) => row(m))
          )}
          {convo.working.length > 0 && (
            <div className="work">
              <div className="work-head">
                <span className="spinner" />
                <span>
                  {convo.working
                    .map((id) => group.members.find((m) => m.id === id)?.name ?? "A bot")
                    .join(", ")}{" "}
                  working…
                </span>
              </div>
            </div>
          )}
          {convo.error && <ErrorNotice>{convo.error}</ErrorNotice>}
        </div>
      </div>

      <GroupComposer
        group={group}
        threadRoot={thread?.id ?? null}
        onSent={() => {
          convo.poke();
          onChanged();
        }}
      />
      {editing && (
        <GroupDialog
          bots={bots}
          group={group}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
          onDeleted={onDeleted}
        />
      )}
    </section>
  );
}

function GroupComposer({
  group,
  threadRoot,
  onSent,
}: {
  group: Group;
  threadRoot: string | null;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [mention, setMention] = useState<{ query: string; at: number } | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const send = useAction(() => postToGroup(group.id, text.trim(), threadRoot), {
    onDone: () => {
      setText("");
      onSent();
    },
  });
  const options = useMemo(() => {
    if (!mention) return [];
    const q = mention.query.toLowerCase();
    return [...group.members.map((m) => m.name), "everyone"].filter((n) =>
      n.toLowerCase().startsWith(q),
    );
  }, [mention, group.members]);

  const pick = (name: string) => {
    if (!mention) return;
    const next = `${text.slice(0, mention.at)}@${name} ${text.slice(mention.at + 1 + mention.query.length)}`;
    setText(next);
    setMention(null);
    ref.current?.focus();
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        {mention && options.length > 0 && (
          <div className="popmenu" role="listbox">
            <div className="popmenu-label">Members</div>
            {options.map((name) => (
              <button
                key={name}
                type="button"
                className="popitem"
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(name);
                }}
              >
                <span>@{name}</span>
              </button>
            ))}
          </div>
        )}
        {threadRoot && <div className="replying">↪ Replying in a thread</div>}
        <textarea
          ref={ref}
          rows={1}
          value={text}
          placeholder={`Message ${group.name} — @ to give a bot the request`}
          onChange={(e) => {
            const value = e.target.value;
            setText(value);
            const upto = value.slice(0, e.target.selectionStart);
            const m = /(^|\s)@([\w-]*)$/.exec(upto);
            setMention(m ? { query: m[2], at: upto.length - m[2].length - 1 } : null);
          }}
          onKeyDown={(e) => {
            if (mention && options.length && (e.key === "Tab" || e.key === "Enter")) {
              e.preventDefault();
              pick(options[0]);
              return;
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (text.trim() && !send.pending) void send.run();
            }
          }}
        />
        <div className="composer-row">
          <span className="composer-hint">
            {send.error ? <span className="attach-error">{send.error}</span> : "Enter to send"}
          </span>
          <button
            type="button"
            className="ibtn send"
            disabled={!text.trim() || send.pending}
            onClick={() => void send.run()}
            aria-label="Send"
          >
            ↑
          </button>
        </div>
      </div>
    </div>
  );
}

/** Create a group, or change one: its name, its two to six bots, and its lead. */
export function GroupDialog({
  bots,
  group,
  onClose,
  onSaved,
  onDeleted,
}: {
  bots: Bot[];
  group?: Group | null;
  onClose: () => void;
  onSaved: (group: Group) => void;
  onDeleted?: () => void;
}) {
  const [name, setName] = useState(group?.name ?? "");
  const [members, setMembers] = useState<string[]>(group?.members.map((m) => m.id) ?? []);
  const [lead, setLead] = useState<string | null>(group?.lead_bot_id ?? null);
  const save = useAction(
    () =>
      group
        ? updateGroup(group.id, { name, members, lead })
        : createGroup(name, members, lead ?? members[0]),
    { onDone: onSaved },
  );
  const remove = useAction(() => deleteGroup(group?.id ?? ""), { onDone: () => onDeleted?.() });
  const toggle = (id: string) =>
    setMembers((list) => (list.includes(id) ? list.filter((m) => m !== id) : [...list, id]));
  const choices = bots.filter((b) => !b.hidden || members.includes(b.id));

  return (
    <Modal title={group ? "Edit group" : "New group"} onClose={onClose}>
      <div className="form">
        <label>
          Name
          <input value={name} autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
        <div>
          <div className="form-label">Bots (2 to 6)</div>
          <div className="group-pick">
            {choices.map((b) => (
              <label key={b.id} className={cx("pick", members.includes(b.id) && "on")}>
                <input
                  type="checkbox"
                  checked={members.includes(b.id)}
                  onChange={() => toggle(b.id)}
                />
                <Avatar bot={b} size={22} />
                {b.name}
                {b.label && <small>{b.label}</small>}
              </label>
            ))}
          </div>
        </div>
        {members.length > 0 && (
          <label>
            Lead <small>— answers a message that names nobody</small>
            <select
              value={lead && members.includes(lead) ? lead : members[0]}
              onChange={(e) => setLead(e.target.value)}
            >
              {members.map((id) => (
                <option key={id} value={id}>
                  {bots.find((b) => b.id === id)?.name ?? id}
                </option>
              ))}
            </select>
          </label>
        )}
        {(save.error || remove.error) && <ErrorNotice>{save.error || remove.error}</ErrorNotice>}
        <div className="form-actions">
          {group && (
            <button
              type="button"
              className="pbtn danger"
              style={{ marginRight: "auto" }}
              onClick={() => {
                if (window.confirm(`Delete the group “${group.name}”? The bots stay.`))
                  void remove.run();
              }}
            >
              Delete group
            </button>
          )}
          <button type="button" className="pbtn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="pbtn primary"
            disabled={!name.trim() || members.length < 2 || members.length > 6 || save.pending}
            onClick={() => void save.run()}
          >
            {group ? "Save" : "Create group"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
