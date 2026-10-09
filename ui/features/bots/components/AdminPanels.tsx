"use client";

import { useCallback, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type OrgPolicy,
  auditEvents,
  deleteSecret,
  deleteTelemetry,
  getPolicy,
  getTelemetry,
  listSecrets,
  makeScimToken,
  putSecret,
  revokeScim,
  savePolicy,
  saveTelemetry,
  scimStatus,
} from "@/lib/api/bots";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";

/**
 * The organization's admin settings, as tabs of the Team dialog: policies, team secrets,
 * provisioning (SCIM), telemetry export and the audit trail. The server holds every
 * rule (`domain/policies.py`, `runtime/enterprise.py`); nothing here is shown a
 * secret's value, a SCIM token after the moment it is made, or telemetry headers.
 */

export function PoliciesPanel() {
  const current = useResource(getPolicy);
  const [draft, setDraft] = useState<(OrgPolicy & { hosts: string }) | null>(null);
  const value =
    draft ??
    (current.data ? { ...current.data, hosts: current.data.allowed_hosts.join("\n") } : null);
  const save = useAction(
    () => {
      if (!value) throw new Error("nothing to save");
      const { hosts, ...policy } = value;
      return savePolicy({
        ...policy,
        allowed_hosts: hosts
          .split(/[\s,]+/)
          .map((h) => h.trim())
          .filter(Boolean),
      });
    },
    {
      onDone: () => {
        setDraft(null);
        current.refresh();
      },
    },
  );
  if (!value) return current.error ? <ErrorNotice>{current.error}</ErrorNotice> : null;
  const set = (patch: Partial<typeof value>) => setDraft({ ...value, ...patch });
  return (
    <div className="form">
      <h3 className="team-h" style={{ marginTop: 0 }}>
        Network
      </h3>
      <label className="check-row">
        <input
          type="radio"
          name="network"
          checked={value.network === "open"}
          onChange={() => set({ network: "open" })}
        />
        <span>Bots may reach any site</span>
      </label>
      <label className="check-row">
        <input
          type="radio"
          name="network"
          checked={value.network === "allowlist"}
          onChange={() => set({ network: "allowlist" })}
        />
        <span>Only these hosts (and their subdomains)</span>
      </label>
      {value.network === "allowlist" && (
        <label>
          <span>
            Allowed hosts <small>— one per line, e.g. github.com</small>
          </span>
          <textarea value={value.hosts} onChange={(e) => set({ hosts: e.target.value })} />
          <small>
            Bots&apos; browsers refuse every other host, apps elsewhere can&apos;t be used, and
            sandboxed commands run without a network.
          </small>
        </label>
      )}
      <h3 className="team-h">For every bot</h3>
      <label className="check-row">
        <input
          type="checkbox"
          checked={value.require_review}
          onChange={(e) => set({ require_review: e.target.checked })}
        />
        <span>Require Auto Review — a second model checks every bot&apos;s risky steps</span>
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={value.template_links}
          onChange={(e) => set({ template_links: e.target.checked })}
        />
        <span>Members may share bots by template link</span>
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={value.members_add_apps}
          onChange={(e) => set({ members_add_apps: e.target.checked })}
        />
        <span>Members may connect apps (otherwise only owners and admins)</span>
      </label>
      {save.error && <ErrorNotice>{save.error}</ErrorNotice>}
      <div className="form-actions">
        <button
          type="button"
          className="pbtn primary"
          disabled={save.pending || draft === null}
          onClick={() => void save.run()}
        >
          {save.pending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

export function SecretsPanel() {
  const secrets = useResource(listSecrets);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const put = useAction(() => putSecret(name.trim(), value), {
    onDone: () => {
      setName("");
      setValue("");
      secrets.refresh();
    },
  });
  const remove = useAction((n: string) => deleteSecret(n), { onDone: secrets.refresh });
  return (
    <div>
      <p className="screen-help" style={{ marginTop: 0 }}>
        Values bots&apos; sandboxed commands read as environment variables — an API token for a
        command-line tool, say. Bots are told only the names; a command that prints a value gets
        [REDACTED]. A value is never shown again once saved.
      </p>
      {secrets.data?.secrets.map((s) => (
        <div key={s.name} className="member-row">
          <code className="grow">${s.name}</code>
          <span className="muted">
            {s.bytes} bytes · {new Date(s.updated_at).toLocaleDateString()}
          </span>
          <button
            type="button"
            className="pbtn danger"
            disabled={remove.pending}
            onClick={() => {
              if (window.confirm(`Delete ${s.name}? Commands that use it will fail.`))
                void remove.run(s.name);
            }}
          >
            Delete
          </button>
        </div>
      ))}
      {secrets.data && secrets.data.secrets.length === 0 && (
        <p className="screen-help">No secrets yet.</p>
      )}
      <h3 className="team-h">Set a secret</h3>
      <div className="form">
        <div className="form-row">
          <label>
            Name
            <input
              value={name}
              placeholder="GITHUB_TOKEN"
              onChange={(e) => setName(e.target.value.toUpperCase())}
            />
          </label>
          <label>
            Value
            <input
              type="password"
              autoComplete="off"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
        </div>
        {(put.error || remove.error || secrets.error) && (
          <ErrorNotice>{put.error || remove.error || secrets.error}</ErrorNotice>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="pbtn primary"
            disabled={put.pending || !name.trim() || !value}
            onClick={() => void put.run()}
          >
            {put.pending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ProvisioningPanel() {
  const status = useResource(scimStatus);
  const [token, setToken] = useState<string | null>(null);
  const make = useAction(makeScimToken, {
    onDone: (made) => {
      setToken(made.token);
      status.refresh();
    },
  });
  const revoke = useAction(revokeScim, {
    onDone: () => {
      setToken(null);
      status.refresh();
    },
  });
  const s = status.data;
  if (!s) return status.error ? <ErrorNotice>{status.error}</ErrorNotice> : null;
  return (
    <div className="form">
      <p className="screen-help" style={{ margin: 0 }}>
        Let your identity provider (Okta, Microsoft Entra, …) create members when people are
        assigned this app, keep their names and emails current, and remove them when they leave.
        Give it the SCIM URL and a token. It must be able to reach this address.
      </p>
      <label>
        SCIM URL
        <input className="input" readOnly value={s.base_url} />
      </label>
      <p className="routine-meta" style={{ margin: 0 }}>
        {s.configured
          ? `A token ending …${s.hint} is set${
              s.last_used_at
                ? `; last used ${new Date(s.last_used_at).toLocaleString()}`
                : "; not used yet"
            }.`
          : "No token yet."}
      </p>
      {token && (
        <div className="link-box">
          <div className="routine-meta">Copy it now — it is not shown again.</div>
          <input
            className="input"
            readOnly
            value={token}
            onFocus={(e) => e.currentTarget.select()}
          />
          <button
            type="button"
            className="pbtn"
            onClick={() => void navigator.clipboard?.writeText(token).catch(() => undefined)}
          >
            Copy
          </button>
        </div>
      )}
      {(make.error || revoke.error) && <ErrorNotice>{make.error || revoke.error}</ErrorNotice>}
      <div className="form-actions">
        {s.configured && (
          <button
            type="button"
            className="pbtn danger"
            disabled={revoke.pending}
            onClick={() => {
              if (window.confirm("Turn off provisioning? The provider's token stops working."))
                void revoke.run();
            }}
          >
            Turn off
          </button>
        )}
        <button
          type="button"
          className="pbtn primary"
          disabled={make.pending}
          onClick={() => {
            if (!s.configured || window.confirm("Replace the token? The old one stops working."))
              void make.run();
          }}
        >
          {s.configured ? "Replace token" : "Make a token"}
        </button>
      </div>
    </div>
  );
}

export function TelemetryPanel() {
  const current = useResource(getTelemetry);
  const [form, setForm] = useState<{
    endpoint: string;
    headers: string;
    include_email: boolean;
    include_actions: boolean;
    enabled: boolean;
  } | null>(null);
  const t = current.data;
  const draft = form ?? {
    endpoint: t?.endpoint ?? "",
    headers: "",
    include_email: t?.include_email ?? false,
    include_actions: t?.include_actions ?? true,
    enabled: t?.enabled ?? true,
  };
  const set = (patch: Partial<typeof draft>) => setForm({ ...draft, ...patch });
  const save = useAction(
    () => {
      const headers: Record<string, string> = {};
      for (const line of draft.headers.split("\n")) {
        const [key, ...rest] = line.split(":");
        if (key.trim() && rest.length) headers[key.trim()] = rest.join(":").trim();
      }
      return saveTelemetry({
        endpoint: draft.endpoint.trim(),
        headers: Object.keys(headers).length ? headers : undefined,
        include_email: draft.include_email,
        include_actions: draft.include_actions,
        enabled: draft.enabled,
      });
    },
    {
      onDone: () => {
        setForm(null);
        current.refresh();
      },
    },
  );
  const remove = useAction(deleteTelemetry, {
    onDone: () => {
      setForm(null);
      current.refresh();
    },
  });
  if (!t) return current.error ? <ErrorNotice>{current.error}</ErrorNotice> : null;
  return (
    <div className="form">
      <p className="screen-help" style={{ margin: 0 }}>
        Send this organization&apos;s audit trail — and every tool call a bot makes, with how it
        ended — to your OpenTelemetry collector as OTLP logs. Never arguments, results, messages or
        files.
      </p>
      <label>
        <span>
          Collector <small>— its OTLP/HTTP address; /v1/logs is added</small>
        </span>
        <input
          value={draft.endpoint}
          placeholder="https://otel-collector.example.com:4318"
          onChange={(e) => set({ endpoint: e.target.value })}
        />
      </label>
      <label>
        <span>
          Headers <small>— one per line, e.g. Authorization: Bearer …</small>
        </span>
        <textarea
          value={draft.headers}
          placeholder={t.has_headers ? "saved — leave empty to keep" : ""}
          onChange={(e) => set({ headers: e.target.value })}
        />
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.include_actions}
          onChange={(e) => set({ include_actions: e.target.checked })}
        />
        <span>Include bots&apos; tool calls</span>
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.include_email}
          onChange={(e) => set({ include_email: e.target.checked })}
        />
        <span>Include members&apos; email addresses</span>
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        <span>On</span>
      </label>
      {t.configured && (
        <p className="routine-meta" style={{ margin: 0 }}>
          {t.last_error
            ? `Last try failed: ${t.last_error}`
            : t.last_sent_at
              ? `Last sent ${new Date(t.last_sent_at).toLocaleString()}`
              : "Nothing sent yet."}
        </p>
      )}
      {(save.error || remove.error) && <ErrorNotice>{save.error || remove.error}</ErrorNotice>}
      <div className="form-actions">
        {t.configured && (
          <button
            type="button"
            className="pbtn danger"
            disabled={remove.pending}
            onClick={() => void remove.run()}
          >
            Remove
          </button>
        )}
        <button
          type="button"
          className="pbtn primary"
          disabled={save.pending || !draft.endpoint.trim()}
          onClick={() => void save.run()}
        >
          {save.pending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

const AREAS: [string, string][] = [
  ["", "Everything"],
  ["member.", "People"],
  ["sso.", "Single sign-on"],
  ["scim.", "Provisioning"],
  ["policy.", "Policies"],
  ["secret.", "Secrets"],
  ["bot.", "Bots"],
  ["template_link.", "Template links"],
  ["connector.", "Apps"],
  ["routine.", "Routines"],
  ["telemetry.", "Telemetry"],
];

export function AuditPanel() {
  const [area, setArea] = useState("");
  const fetcher = useCallback((signal: AbortSignal) => auditEvents(area, signal), [area]);
  const events = useResource(fetcher, { intervalMs: 15_000 });
  return (
    <div>
      <div className="form" style={{ marginBottom: 8 }}>
        <label>
          Show
          <select value={area} onChange={(e) => setArea(e.target.value)}>
            {AREAS.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {events.data?.events.map((e) => (
        <div key={e.id} className="audit-row">
          <div>
            <code>{e.action}</code> {e.target && <strong>{e.target}</strong>}
          </div>
          <div className="routine-meta">
            {new Date(e.occurred_at).toLocaleString()} · {e.actor || "—"}
            {Object.keys(e.detail).length > 0 && ` · ${summary(e.detail)}`}
          </div>
        </div>
      ))}
      {events.data && events.data.events.length === 0 && (
        <p className="screen-help">Nothing yet.</p>
      )}
      {events.error && <ErrorNotice>{events.error}</ErrorNotice>}
    </div>
  );
}

function summary(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .map(([k, v]) => {
      if (v && typeof v === "object" && "from" in v && "to" in v) {
        const change = v as { from: unknown; to: unknown };
        return `${k}: ${JSON.stringify(change.from)} → ${JSON.stringify(change.to)}`;
      }
      return `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`;
    })
    .join(", ")
    .slice(0, 240);
}
