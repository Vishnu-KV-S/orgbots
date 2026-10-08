"use client";

import { useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Connector,
  type MarketConnector,
  addConnector,
  deleteConnector,
  installConnector,
  listConnectors,
  listMarketConnectors,
  refreshConnector,
  updateConnector,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";

/**
 * Apps every bot can call directly — MCP servers ("connectors"), from the marketplace
 * or by address. Adding one connects to it first, so an address or a token that does
 * not work is caught here, not by a bot mid-task. Tokens are sealed on the server and
 * never shown again. A tool marked "reads" runs; anything else asks you first, unless
 * you allow that app in a bot's rules.
 */
export function AppsPane() {
  const apps = useResource(listConnectors, { intervalMs: 15_000 });
  const [view, setView] = useState<"apps" | "market" | "add">("apps");
  const list = apps.data?.connectors ?? [];

  return (
    <div className="apps">
      <div className="bpane-tabs" style={{ padding: 0, border: "none", marginBottom: 10 }}>
        <button
          type="button"
          className={cx("tab", view === "apps" && "on")}
          onClick={() => setView("apps")}
        >
          Connected{list.length ? ` · ${list.length}` : ""}
        </button>
        <button
          type="button"
          className={cx("tab", view === "market" && "on")}
          onClick={() => setView("market")}
        >
          Marketplace
        </button>
        <span style={{ flex: 1 }} />
        <button type="button" className="pbtn" onClick={() => setView("add")}>
          + By address
        </button>
      </div>
      {view === "market" ? (
        <Market
          onInstalled={() => {
            apps.refresh();
            setView("apps");
          }}
        />
      ) : view === "add" ? (
        <AddApp
          onDone={() => {
            apps.refresh();
            setView("apps");
          }}
          onCancel={() => setView("apps")}
        />
      ) : (
        <>
          <p className="screen-help" style={{ marginTop: 0 }}>
            Apps your bots call directly instead of using their websites. Every bot can use every
            app here; a tool that changes something asks you first unless you allow the app in a
            bot&apos;s rules.
          </p>
          {list.map((c) => (
            <AppRow key={c.id} app={c} onChanged={apps.refresh} />
          ))}
          {apps.data && list.length === 0 && (
            <p className="screen-help">No apps yet — try the Marketplace.</p>
          )}
          {apps.error && <ErrorNotice>{apps.error}</ErrorNotice>}
        </>
      )}
    </div>
  );
}

function AppRow({ app, onChanged }: { app: Connector; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const toggle = useAction(() => updateConnector(app.id, { enabled: !app.enabled }), {
    onDone: onChanged,
  });
  const refresh = useAction(() => refreshConnector(app.id), { onDone: onChanged });
  const remove = useAction(() => deleteConnector(app.id), { onDone: onChanged });
  const error = toggle.error || refresh.error || remove.error;
  return (
    <div className="app-row">
      <div className="app-head">
        <span
          className={cx("run-dot", app.status === "ok" && app.enabled ? "started" : "refused")}
        />
        <button type="button" className="linklike app-name" onClick={() => setOpen((o) => !o)}>
          {app.title}
        </button>
        <code className="muted">{app.name}</code>
        <span style={{ flex: 1 }} />
        <label className="creds-auto" title="Bots may call this app">
          <input
            type="checkbox"
            checked={app.enabled}
            disabled={toggle.pending}
            onChange={() => void toggle.run()}
          />
          On
        </label>
      </div>
      <div className="routine-meta">
        {app.tools.length} tools
        {app.server_name && ` · ${app.server_name}`}
        {app.has_token ? " · token set" : ""}
        {app.status === "error" && ` · last try failed: ${app.last_error}`}
      </div>
      {open && (
        <>
          <ul className="app-tools">
            {app.tools.map((t) => (
              <li key={t.name}>
                <code>{t.name}</code>
                {t.read_only ? <span className="skill-badge ok">reads</span> : null}
                {t.description && <span className="muted"> — {t.description}</span>}
              </li>
            ))}
          </ul>
          <div className="form-actions" style={{ justifyContent: "flex-start" }}>
            <button
              type="button"
              className="pbtn"
              disabled={refresh.pending}
              onClick={() => void refresh.run()}
            >
              {refresh.pending ? "Connecting…" : "Reconnect & refresh tools"}
            </button>
            <button
              type="button"
              className="pbtn danger"
              disabled={remove.pending}
              onClick={() => {
                if (window.confirm(`Remove ${app.title}? Bots will no longer be able to call it.`))
                  void remove.run();
              }}
            >
              Remove
            </button>
          </div>
        </>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
    </div>
  );
}

function Market({ onInstalled }: { onInstalled: () => void }) {
  const market = useResource(listMarketConnectors);
  const [tokens, setTokens] = useState<Record<string, string>>({});
  const install = useAction((key: string) => installConnector(key, tokens[key]), {
    onDone: () => {
      market.refresh();
      onInstalled();
    },
  });
  return (
    <div>
      <p className="screen-help" style={{ marginTop: 0 }}>
        Remote MCP servers run by the apps&apos; makers. Ones marked “checked” have been connected
        to from this runtime; the others are as their makers document them.
      </p>
      {market.data?.connectors.map((m: MarketConnector) => (
        <div key={m.key} className="market-item">
          <div className="market-head">
            <strong>{m.title}</strong>
            <span className="muted">{m.category}</span>
            {m.checked && <span className="skill-badge ok">checked</span>}
            <span style={{ flex: 1 }} />
            {m.installed ? (
              <span className="skill-badge ok">Connected</span>
            ) : (
              <button
                type="button"
                className="pbtn"
                disabled={install.pending || (m.needs_token && !tokens[m.key]?.trim())}
                onClick={() => void install.run(m.key)}
              >
                Connect
              </button>
            )}
          </div>
          <div className="market-blurb">{m.blurb}</div>
          {m.takes_token && !m.installed && (
            <input
              className="input"
              type="password"
              autoComplete="off"
              style={{ width: "100%", marginTop: 6 }}
              placeholder={m.key_help}
              value={tokens[m.key] ?? ""}
              onChange={(e) => setTokens((t) => ({ ...t, [m.key]: e.target.value }))}
            />
          )}
        </div>
      ))}
      {install.error && <ErrorNotice>{install.error}</ErrorNotice>}
      {market.error && <ErrorNotice>{market.error}</ErrorNotice>}
    </div>
  );
}

function AddApp({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("https://");
  const [auth, setAuth] = useState<Connector["auth_kind"]>("none");
  const [header, setHeader] = useState("");
  const [token, setToken] = useState("");
  const add = useAction(
    () =>
      addConnector({
        title,
        url,
        auth_kind: auth,
        header_name: header,
        token: auth === "none" ? undefined : token,
      }),
    { onDone },
  );
  return (
    <div className="form">
      <label>
        Name
        <input
          value={title}
          placeholder="Company wiki"
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label>
        <span>
          MCP server address <small>— its Streamable HTTP endpoint, often ending in /mcp</small>
        </span>
        <input value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <div className="form-row">
        <label>
          Sign-in
          <select value={auth} onChange={(e) => setAuth(e.target.value as Connector["auth_kind"])}>
            <option value="none">None</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Token in a header</option>
          </select>
        </label>
        {auth === "header" && (
          <label>
            Header
            <input
              value={header}
              placeholder="X-API-Key"
              onChange={(e) => setHeader(e.target.value)}
            />
          </label>
        )}
      </div>
      {auth !== "none" && (
        <label>
          Token <small>— sealed on the server, never shown again</small>
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </label>
      )}
      {add.error && <ErrorNotice>{add.error}</ErrorNotice>}
      <div className="form-actions">
        <button type="button" className="pbtn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="pbtn primary"
          disabled={add.pending || !title.trim() || url.length < 12}
          onClick={() => void add.run()}
        >
          {add.pending ? "Connecting…" : "Connect"}
        </button>
      </div>
    </div>
  );
}
