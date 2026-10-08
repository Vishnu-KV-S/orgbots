"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Bot,
  type FileRevision,
  type TeamFile,
  createFile,
  deleteFile,
  getFile,
  listFileRevisions,
  listFiles,
  lockFile,
  moveFile,
  restoreFile,
  saveFile,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import { timeAgo } from "../lib/text";

/**
 * The team's shared drive: every file the bots on this team (a bot and the helpers
 * under it) have saved for each other, organised in folders. The person sees what the
 * bots see, and more — who changed each file and when, every earlier version, and the
 * trash — and can add, upload, edit, move, lock and restore. A save names the version
 * it was made on, so saving over a bot's newer change is refused rather than silently
 * undoing it; a locked file is one no bot may change.
 */

const MAX_CHARS = 100_000;

const OP_LABEL: Record<FileRevision["op"], string> = {
  create: "created",
  write: "rewrote",
  append: "added to",
  edit: "edited",
  move: "moved",
  delete: "deleted",
  restore: "restored",
};

function who(kind: string, name: string): string {
  return kind === "person" ? "you" : name || "a bot";
}

function ago(iso: string): string {
  const t = timeAgo(iso);
  return t === "now" ? "just now" : `${t} ago`;
}

function size(chars: number): string {
  return chars < 1000 ? `${chars} chars` : `${(chars / 1000).toFixed(1)}k chars`;
}

type Folder = { name: string; path: string; folders: Folder[]; files: TeamFile[] };

function tree(files: TeamFile[]): Folder {
  const root: Folder = { name: "", path: "/", folders: [], files: [] };
  for (const f of files) {
    const parts = f.path.split("/").filter(Boolean);
    let at = root;
    parts.slice(0, -1).forEach((part, i) => {
      const path = `/${parts.slice(0, i + 1).join("/")}`;
      let next = at.folders.find((d) => d.path.toLowerCase() === path.toLowerCase());
      if (!next) {
        next = { name: part, path, folders: [], files: [] };
        at.folders.push(next);
      }
      at = next;
    });
    at.files.push(f);
  }
  const sort = (d: Folder) => {
    d.folders.sort((a, b) => a.name.localeCompare(b.name));
    d.files.sort((a, b) => a.name.localeCompare(b.name));
    d.folders.forEach(sort);
  };
  sort(root);
  return root;
}

function count(d: Folder): number {
  return d.files.length + d.folders.reduce((n, c) => n + count(c), 0);
}

function download(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

async function readText(file: File): Promise<string> {
  const text = await file.text();
  if (text.includes("\u0000")) throw new Error(`${file.name} is not a text file`);
  if (text.length > MAX_CHARS)
    throw new Error(`${file.name} is over ${MAX_CHARS.toLocaleString()} characters`);
  return text;
}

function folderOf(path: string): string {
  const p = path.trim();
  if (!p || p.endsWith("/")) return p.replace(/\/+$/, "");
  return p.slice(0, p.lastIndexOf("/"));
}

export function FilesPane({
  bot,
  openPath,
  onOpened,
}: {
  bot: Bot;
  /** A file to open, from an activity line in the conversation. */
  openPath: string | null;
  onOpened: () => void;
}) {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setQ(query), 250);
    return () => clearTimeout(timer);
  }, [query]);
  const fetcher = useCallback((signal: AbortSignal) => listFiles(bot.id, q, signal), [bot.id, q]);
  const drive = useResource(fetcher, { intervalMs: 5000 });
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [showTrash, setShowTrash] = useState(false);

  const files = useMemo(() => drive.data?.files ?? [], [drive.data]);
  const trash = drive.data?.trash ?? [];
  const members = drive.data?.team.members ?? [];
  const root = useMemo(() => tree(files), [files]);

  // A file named in the conversation: open it, or — if the listing predates it — look
  // once more before giving up.
  const retried = useRef(false);
  useEffect(() => {
    if (!openPath || !drive.data) return;
    const hit = drive.data.files.find((f) => f.path.toLowerCase() === openPath.toLowerCase());
    if (hit) setOpenId(hit.id);
    if (hit || retried.current) {
      retried.current = false;
      onOpened();
    } else {
      retried.current = true;
      drive.refresh();
    }
  }, [openPath, drive.data, drive.refresh, onOpened]);

  const restore = useAction((id: string) => restoreFile(bot.id, id), { onDone: drive.refresh });

  if (openId) {
    return (
      <FileView
        key={openId}
        bot={bot}
        fileId={openId}
        onClose={() => setOpenId(null)}
        onChanged={drive.refresh}
      />
    );
  }

  const toggle = (path: string) =>
    setClosed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const renderFolder = (d: Folder) => (
    <ul className="ftree" key={d.path}>
      {d.folders.map((sub) => (
        <li key={sub.path}>
          <button type="button" className="frow folder" onClick={() => toggle(sub.path)}>
            <span className="fico">{closed.has(sub.path) ? "▸" : "▾"}</span>
            <span className="fname">{sub.name}</span>
            <span className="fmeta">{count(sub)}</span>
          </button>
          {!closed.has(sub.path) && renderFolder(sub)}
        </li>
      ))}
      {d.files.map((f) => (
        <li key={f.id}>
          <button type="button" className="frow" onClick={() => setOpenId(f.id)} title={f.path}>
            <span className="fico">{f.locked ? "🔒" : "▤"}</span>
            <span className="fname">{f.name}</span>
            <span className="fmeta">
              {who(f.updated_by_kind, f.updated_by_name)} · {timeAgo(f.updated_at)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );

  return (
    <section className="dsec files">
      <h3>
        Team files <span className="count">{files.length}</span>
      </h3>
      <p className="screen-help" style={{ marginTop: 0 }}>
        {members.length > 1
          ? `Shared by ${members.map((m) => m.name).join(", ")}.`
          : `${bot.name}'s team drive.`}{" "}
        Every bot on the team reads and writes these files — research, tables, drafts — and hands
        work to the others through them.
      </p>

      <div className="files-bar">
        <input
          type="search"
          className="urlbox"
          value={query}
          placeholder="Search names and contents…"
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="button" className="pbtn" onClick={() => setAdding((a) => !a)}>
          {adding ? "Close" : "+ Add"}
        </button>
      </div>

      {adding && (
        <AddFile
          bot={bot}
          onDone={(file) => {
            setAdding(false);
            drive.refresh();
            if (file) setOpenId(file.id);
          }}
        />
      )}

      {drive.error && !drive.data && <ErrorNotice>{drive.error}</ErrorNotice>}

      {q.trim() ? (
        <ul className="ftree fmatches">
          {(drive.data?.matches ?? []).map((f) => (
            <li key={f.id}>
              <button type="button" className="frow match" onClick={() => setOpenId(f.id)}>
                <span className="fname mono">{f.path}</span>
                {f.snippet && <span className="snippet">{f.snippet}</span>}
              </button>
            </li>
          ))}
          {drive.data && (drive.data.matches ?? []).length === 0 && (
            <li className="screen-help">Nothing matches “{q.trim()}”.</li>
          )}
        </ul>
      ) : files.length > 0 ? (
        renderFolder(root)
      ) : (
        drive.data && (
          <p className="screen-help">
            No files yet. Bots save what they find here as they work — notes, tables, drafts —
            and pass them to each other. You can add or upload files for them too.
          </p>
        )
      )}

      {trash.length > 0 && (
        <div className="ftrash">
          <button type="button" className="linklike" onClick={() => setShowTrash((s) => !s)}>
            {showTrash ? "▾" : "▸"} Recently deleted ({trash.length})
          </button>
          {showTrash && (
            <ul className="ftree">
              {trash.map((f) => (
                <li key={f.id} className="frow static">
                  <span className="fname mono">{f.path}</span>
                  <span className="fmeta">
                    {who(f.updated_by_kind, f.updated_by_name)} · {timeAgo(f.deleted_at ?? "")}
                  </span>
                  <button
                    type="button"
                    className="linklike"
                    disabled={restore.pending}
                    onClick={() => void restore.run(f.id)}
                  >
                    Restore
                  </button>
                </li>
              ))}
            </ul>
          )}
          {restore.error && <ErrorNotice>{restore.error}</ErrorNotice>}
        </div>
      )}
    </section>
  );
}

function AddFile({ bot, onDone }: { bot: Bot; onDone: (file: TeamFile | null) => void }) {
  const [path, setPath] = useState("/");
  const [content, setContent] = useState("");
  const picker = useRef<HTMLInputElement>(null);
  const create = useAction(() => createFile(bot.id, path, content), {
    onDone: (r) => onDone(r.file),
  });
  const upload = useAction(
    async (list: File[]) => {
      const folder = folderOf(path);
      for (const file of list) {
        await createFile(bot.id, `${folder}/${file.name}`, await readText(file));
      }
      return list.length;
    },
    { onDone: () => onDone(null) },
  );
  const error = create.error || upload.error;
  return (
    <div className="form fadd">
      <label>
        Path
        <input
          className="mono"
          value={path}
          placeholder="/projects/acme/notes.md"
          onChange={(e) => setPath(e.target.value)}
          autoFocus
        />
        <small>Folders are part of the path. Uploads go into the folder named here.</small>
      </label>
      <label>
        Content
        <textarea
          rows={6}
          className="mono"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="Notes, a CSV table, a draft…"
        />
      </label>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      <div className="form-actions">
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          accept=".txt,.md,.markdown,.csv,.tsv,.json,.yaml,.yml,.xml,.html,.log,text/*"
          onChange={(e) => {
            const list = Array.from(e.target.files ?? []);
            e.target.value = "";
            if (list.length) void upload.run(list);
          }}
        />
        <button
          type="button"
          className="pbtn"
          disabled={upload.pending}
          onClick={() => picker.current?.click()}
        >
          {upload.pending ? "Uploading…" : "Upload files…"}
        </button>
        <span style={{ flex: 1 }} />
        <button type="button" className="pbtn" onClick={() => onDone(null)}>
          Cancel
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={!path.trim() || path.trim().endsWith("/") || create.pending}
          onClick={() => void create.run()}
        >
          Create
        </button>
      </div>
    </div>
  );
}

function FileView({
  bot,
  fileId,
  onClose,
  onChanged,
}: {
  bot: Bot;
  fileId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [history, setHistory] = useState(false);
  const [draft, setDraft] = useState("");
  const [newPath, setNewPath] = useState("");
  const fetcher = useCallback((signal: AbortSignal) => getFile(bot.id, fileId, signal), [
    bot.id,
    fileId,
  ]);
  // Polled, so a bot's change shows up while the person reads — but not while they
  // edit, when the version they opened is the one their save is checked against.
  const file = useResource(fetcher, { intervalMs: 5000, enabled: !editing });
  const f = file.data;

  const changed = () => {
    file.refresh();
    onChanged();
  };
  const save = useAction(() => saveFile(bot.id, fileId, draft, f?.version ?? 0), {
    onDone: () => {
      setEditing(false);
      changed();
    },
  });
  const rename = useAction(() => moveFile(bot.id, fileId, newPath), {
    onDone: () => {
      setRenaming(false);
      changed();
    },
  });
  const lock = useAction((locked: boolean) => lockFile(bot.id, fileId, locked), {
    onDone: changed,
  });
  const remove = useAction(() => deleteFile(bot.id, fileId, f?.version), {
    onDone: () => {
      onChanged();
      onClose();
    },
  });
  const undelete = useAction(() => restoreFile(bot.id, fileId), { onDone: changed });

  const back = (
    <button type="button" className="linklike" onClick={onClose}>
      ← All files
    </button>
  );
  if (!f) {
    return (
      <section className="dsec files">
        {back}
        {file.error ? <ErrorNotice>{file.error}</ErrorNotice> : <p className="screen-help">…</p>}
      </section>
    );
  }

  const error = save.error || rename.error || lock.error || remove.error || undelete.error;
  return (
    <section className="dsec files">
      {back}
      <div className="fpath">
        {f.path}
        {f.locked && <span title="Locked: bots can read it but not change it"> 🔒</span>}
      </div>
      <p className="fmeta-line">
        v{f.version} · {size(f.chars)} · changed by {who(f.updated_by_kind, f.updated_by_name)}{" "}
        {ago(f.updated_at)}
        {f.created_by_name &&
          ` · created by ${who(f.created_by_kind, f.created_by_name)} ${ago(f.created_at)}`}
      </p>

      {f.deleted_at ? (
        <div className="form-actions" style={{ justifyContent: "flex-start" }}>
          <span className="screen-help">This file is in the trash.</span>
          <button
            type="button"
            className="pbtn primary"
            disabled={undelete.pending}
            onClick={() => void undelete.run()}
          >
            Restore
          </button>
        </div>
      ) : (
        !editing &&
        !renaming && (
          <div className="ftools">
            <button
              type="button"
              className="linklike"
              onClick={() => {
                setDraft(f.content ?? "");
                save.reset();
                setEditing(true);
              }}
            >
              Edit
            </button>
            <button
              type="button"
              className="linklike"
              onClick={() => {
                setNewPath(f.path);
                rename.reset();
                setRenaming(true);
              }}
            >
              Rename / move
            </button>
            <button
              type="button"
              className="linklike"
              aria-pressed={f.locked}
              title="A locked file can be read by the bots but not changed, moved or deleted"
              disabled={lock.pending}
              onClick={() => void lock.run(!f.locked)}
            >
              {f.locked ? "Unlock" : "Lock"}
            </button>
            <button
              type="button"
              className="linklike"
              onClick={() => download(f.name, f.content ?? "")}
            >
              Download
            </button>
            <button
              type="button"
              className="linklike"
              aria-pressed={history}
              onClick={() => setHistory((h) => !h)}
            >
              History
            </button>
            <button
              type="button"
              className="linklike"
              disabled={remove.pending}
              onClick={() => {
                if (window.confirm(`Delete ${f.path}? It goes to Recently deleted.`))
                  void remove.run();
              }}
            >
              Delete
            </button>
          </div>
        )
      )}

      {renaming && (
        <div className="form">
          <label>
            New path
            <input
              className="mono"
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
              autoFocus
            />
            <small>End it with / to move the file into that folder.</small>
          </label>
          <div className="form-actions">
            <button type="button" className="pbtn" onClick={() => setRenaming(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="pbtn primary"
              disabled={!newPath.trim() || newPath.trim() === f.path || rename.pending}
              onClick={() => void rename.run()}
            >
              Move
            </button>
          </div>
        </div>
      )}

      {editing ? (
        <div className="form fedit">
          <textarea
            className="mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          {save.status === 409 && (
            <p className="screen-help">
              Copy what you want to keep, then reload to see the newer version.{" "}
              <button
                type="button"
                className="linklike"
                onClick={() => {
                  setEditing(false);
                  changed();
                }}
              >
                Reload (discard my edit)
              </button>
            </p>
          )}
          <div className="form-actions">
            <span className="note">{draft.length.toLocaleString()} chars</span>
            <button type="button" className="pbtn" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="pbtn primary"
              disabled={save.pending || draft.length > MAX_CHARS || draft === f.content}
              onClick={() => void save.run()}
            >
              Save
            </button>
          </div>
        </div>
      ) : f.content ? (
        <pre className="fbody">{f.content}</pre>
      ) : (
        <p className="screen-help">(empty)</p>
      )}

      {error && <ErrorNotice>{error}</ErrorNotice>}
      {history && <FileHistory bot={bot} fileId={fileId} current={f.version} onRestored={changed} />}
    </section>
  );
}

function FileHistory({
  bot,
  fileId,
  current,
  onRestored,
}: {
  bot: Bot;
  fileId: string;
  current: number;
  onRestored: () => void;
}) {
  // `current` is a dependency so a new version is listed as soon as it is shown.
  const fetcher = useCallback(
    (signal: AbortSignal) => listFileRevisions(bot.id, fileId, signal),
    [bot.id, fileId, current],
  );
  const revisions = useResource(fetcher);
  const [shown, setShown] = useState<number | null>(null);
  const restore = useAction((version: number) => restoreFile(bot.id, fileId, version), {
    onDone: onRestored,
  });
  return (
    <div className="history">
      <h3>History</h3>
      {revisions.error && <ErrorNotice>{revisions.error}</ErrorNotice>}
      <ul className="revisions">
        {(revisions.data?.revisions ?? []).map((r) => (
          <li key={r.id}>
            <div className="rev-head">
              <strong>v{r.version}</strong>
              <span>
                {who(r.editor_kind, r.editor_name)} {OP_LABEL[r.op]} it
              </span>
              <span className="muted">{ago(r.created_at)}</span>
              <button
                type="button"
                className="linklike"
                onClick={() => setShown(shown === r.version ? null : r.version)}
              >
                {shown === r.version ? "Hide" : "View"}
              </button>
              {r.version !== current && r.op !== "delete" && (
                <button
                  type="button"
                  className="linklike"
                  disabled={restore.pending}
                  onClick={() => void restore.run(r.version)}
                >
                  Restore
                </button>
              )}
            </div>
            {r.note && <span className="why">{r.note}</span>}
            {shown === r.version && <pre className="fbody">{r.content || "(empty)"}</pre>}
          </li>
        ))}
      </ul>
      {restore.error && <ErrorNotice>{restore.error}</ErrorNotice>}
    </div>
  );
}
