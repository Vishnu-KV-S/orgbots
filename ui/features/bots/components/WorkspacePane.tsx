"use client";

import { useCallback, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type CommandResult,
  listWorkspace,
  runSandboxCommand,
  uploadToWorkspace,
  workspaceFileUrl,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The computer's shared `/workspace` — every bot's working folder, where browser downloads
 * land — and a shell in the same sandbox the bots use, for setting things up for them.
 *
 * This shell is always the sandbox. A bot runs a command on this machine only with your
 * approval, in its conversation; there is no button for it here.
 */
export function WorkspacePane() {
  const [path, setPath] = useState("/workspace");
  const fetcher = useCallback((signal: AbortSignal) => listWorkspace(path, signal), [path]);
  const listing = useResource(fetcher, { intervalMs: 5000 });
  const [command, setCommand] = useState("");
  const [history, setHistory] = useState<{ command: string; result: CommandResult }[]>([]);
  const picker = useRef<HTMLInputElement>(null);

  const run = useAction((cmd: string) => runSandboxCommand(cmd), {
    onDone: (result) => {
      setHistory((h) => [...h.slice(-19), { command, result }]);
      setCommand("");
      listing.refresh();
    },
  });
  const upload = useAction(
    async (files: File[]) => {
      for (const f of files) await uploadToWorkspace(path, f);
      return files.length;
    },
    { onDone: listing.refresh },
  );

  const parts = path.split("/").filter(Boolean);
  return (
    <div className="workspace">
      <div className="ws-crumbs">
        {parts.map((part, i) => {
          const to = `/${parts.slice(0, i + 1).join("/")}`;
          return (
            <span key={to}>
              {i > 0 && " / "}
              {to === path ? (
                <strong>{part}</strong>
              ) : (
                <button type="button" className="linklike" onClick={() => setPath(to)}>
                  {part}
                </button>
              )}
            </span>
          );
        })}
        <span style={{ flex: 1 }} />
        <input
          ref={picker}
          type="file"
          multiple
          hidden
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
          {upload.pending ? "Uploading…" : "Upload here"}
        </button>
      </div>

      <div className="ws-list">
        {listing.data?.entries.map((e) =>
          e.folder ? (
            <button key={e.path} type="button" className="ws-row" onClick={() => setPath(e.path)}>
              <span className="ws-ico">▸</span>
              <span className="grow">{e.name}/</span>
            </button>
          ) : (
            <a key={e.path} className="ws-row" href={workspaceFileUrl(e.path)} download={e.name}>
              <span className="ws-ico">{e.link ? "↪" : "·"}</span>
              <span className="grow">{e.name}</span>
              <span className="muted">{e.link ? "link" : bytes(e.bytes)}</span>
            </a>
          ),
        )}
        {listing.data?.entries.length === 0 && <p className="screen-help">Empty.</p>}
      </div>
      {(listing.error || upload.error) && (
        <ErrorNotice>{listing.error || upload.error}</ErrorNotice>
      )}

      <div className="ws-shell">
        {history.map((h, i) => (
          <div key={i} className="ws-cmd">
            <div className="ws-prompt">$ {h.command}</div>
            <pre className="cmd-out">
              {[h.result.stdout, h.result.stderr].filter(Boolean).join("\n") || "(no output)"}
              {h.result.timed_out
                ? "\n(timed out)"
                : h.result.exit_code
                  ? `\n(exit ${h.result.exit_code})`
                  : ""}
            </pre>
          </div>
        ))}
        <div className="typebar">
          <input
            className="urlbox"
            value={command}
            placeholder="Run a command in the sandbox, e.g. pip install --user pandas"
            onChange={(e) => setCommand(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && command.trim() && !run.pending) void run.run(command);
            }}
          />
          <button
            type="button"
            className="pbtn"
            disabled={!command.trim() || run.pending}
            onClick={() => void run.run(command)}
          >
            {run.pending ? "Running…" : "Run"}
          </button>
        </div>
        <p className="screen-help">
          The same sandbox your bots use: /workspace and the network, nothing else of this machine.
        </p>
        {run.error && <ErrorNotice>{run.error}</ErrorNotice>}
      </div>
    </div>
  );
}
