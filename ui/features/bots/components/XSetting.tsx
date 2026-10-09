"use client";

import { useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  connectX,
  disconnectX,
  listBots,
  xChooseBot,
  xLinkCode,
  xStatus,
  xUnlink,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { isAdmin, useMe } from "../lib/me";

/**
 * Tag @bot on X. A person links their X account by posting a one-time code from it —
 * proof it is theirs — and then a post that tags the organization's account becomes a
 * task for the bot they chose. X hears only that the bot has it; the work stays here.
 * Owners and admins connect the organization's account; its tokens are never shown.
 */
export function XSetting() {
  const me = useMe();
  const admin = isAdmin(me);
  const status = useResource(xStatus, { intervalMs: 10_000 });
  const bots = useResource(listBots);
  const [code, setCode] = useState<{ code: string; post: string } | null>(null);
  const makeCode = useAction(xLinkCode, { onDone: setCode });
  const choose = useAction((id: string) => xChooseBot(id), { onDone: status.refresh });
  const unlink = useAction(xUnlink, {
    onDone: () => {
      setCode(null);
      status.refresh();
    },
  });
  const s = status.data;
  if (!s) return status.error ? <ErrorNotice>{status.error}</ErrorNotice> : null;
  const own = (bots.data?.bots ?? []).filter(
    (b) => !b.parent_bot_id && (me === null || b.owner_member_id === me.id),
  );
  const error = makeCode.error || choose.error || unlink.error;

  return (
    <div>
      {!s.account ? (
        <p className="screen-help" style={{ margin: 0 }}>
          Give a bot a task by tagging your organization&apos;s X account in a post.{" "}
          {admin
            ? "Connect the account below first."
            : "An admin has to connect the account first."}
        </p>
      ) : s.link ? (
        <div className="form">
          <p className="screen-help" style={{ margin: 0 }}>
            Linked to <strong>@{s.link.handle}</strong>. Tag <strong>@{s.account}</strong> in a post
            or a reply and your bot gets it — with the post you replied to and any you quoted. X
            only hears that it&apos;s on it; the results are here.
          </p>
          <label>
            Your tags go to
            <select
              value={s.link.bot_id ?? ""}
              disabled={choose.pending}
              onChange={(e) => e.target.value && void choose.run(e.target.value)}
            >
              <option value="">{own[0] ? `${own[0].name} (your first bot)` : "—"}</option>
              {own.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </label>
          <div>
            <button
              type="button"
              className="linklike"
              disabled={unlink.pending}
              onClick={() => void unlink.run()}
            >
              Unlink @{s.link.handle}
            </button>
          </div>
        </div>
      ) : code ? (
        <div className="form">
          <p className="screen-help" style={{ margin: 0 }}>
            Post this from your X account within the hour — it proves the account is yours. This
            page notices within a minute.
          </p>
          <div className="link-box">
            <input
              className="input"
              readOnly
              value={code.post}
              onFocus={(e) => e.currentTarget.select()}
            />
            <a
              className="pbtn"
              href={`https://x.com/intent/post?text=${encodeURIComponent(code.post)}`}
              target="_blank"
              rel="noreferrer"
            >
              Open X
            </a>
          </div>
        </div>
      ) : (
        <div className="form">
          <p className="screen-help" style={{ margin: 0 }}>
            Tag <strong>@{s.account}</strong> on X to give your bot a task. First, link your X
            account.
          </p>
          <div>
            <button
              type="button"
              className="pbtn"
              disabled={makeCode.pending}
              onClick={() => void makeCode.run()}
            >
              Link my X account
            </button>
          </div>
        </div>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {admin && <XAccount status={s} onChanged={status.refresh} />}
    </div>
  );
}

function XAccount({
  status,
  onChanged,
}: {
  status: Awaited<ReturnType<typeof xStatus>>;
  onChanged: () => void;
}) {
  const [handle, setHandle] = useState(status.account ?? "");
  const [read, setRead] = useState("");
  const [post, setPost] = useState("");
  const [open, setOpen] = useState(!status.account);
  const save = useAction(
    () =>
      connectX({
        handle: handle.trim(),
        read_token: read || undefined,
        post_token: post || undefined,
      }),
    {
      onDone: () => {
        setRead("");
        setPost("");
        setOpen(false);
        onChanged();
      },
    },
  );
  const remove = useAction(disconnectX, { onDone: onChanged });
  const info = status.admin;
  return (
    <div style={{ marginTop: 12 }}>
      {status.account && info && (
        <p className="routine-meta" style={{ margin: "0 0 6px" }}>
          @{status.account} · {info.can_reply ? "replies on X" : "does not reply on X"} ·{" "}
          {info.last_error
            ? `last read failed: ${info.last_error}`
            : info.last_polled_at
              ? `read ${new Date(info.last_polled_at).toLocaleTimeString()}`
              : "not read yet"}{" "}
          ·{" "}
          <button type="button" className="linklike" onClick={() => setOpen((o) => !o)}>
            {open ? "close" : "change"}
          </button>{" "}
          ·{" "}
          <button
            type="button"
            className="linklike"
            disabled={remove.pending}
            onClick={() => {
              if (window.confirm(`Disconnect @${status.account}? Tags stop reaching bots.`))
                void remove.run();
            }}
          >
            disconnect
          </button>
        </p>
      )}
      {open && (
        <div className="form">
          <label>
            Organization&apos;s X account
            <input
              value={handle}
              placeholder="@AcmeBots"
              onChange={(e) => setHandle(e.target.value)}
            />
          </label>
          <div className="form-row">
            <label>
              <span>
                Read token <small>— an app bearer token</small>
              </span>
              <input
                type="password"
                autoComplete="off"
                value={read}
                placeholder={status.account ? "saved — leave empty to keep" : ""}
                onChange={(e) => setRead(e.target.value)}
              />
            </label>
            <label>
              <span>
                Post token <small>— optional, to reply</small>
              </span>
              <input
                type="password"
                autoComplete="off"
                value={post}
                placeholder={info?.can_reply ? "saved — leave empty to keep" : ""}
                onChange={(e) => setPost(e.target.value)}
              />
            </label>
          </div>
          {(save.error || remove.error) && <ErrorNotice>{save.error || remove.error}</ErrorNotice>}
          <div className="form-actions">
            <button
              type="button"
              className="pbtn primary"
              disabled={save.pending || !handle.trim()}
              onClick={() => void save.run()}
            >
              {save.pending ? "Checking with X…" : "Connect"}
            </button>
          </div>
        </div>
      )}
      {info && info.recent.length > 0 && (
        <>
          <h3 className="team-h">Recent tags</h3>
          {info.recent.slice(0, 8).map((r) => (
            <div key={r.post_id} className="routine-meta">
              @{r.author || "?"} · {r.outcome}
              {r.note && ` — ${r.note}`} · {new Date(r.at).toLocaleString()}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
