"use client";

import { useCallback, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type Me,
  type Role,
  type TeamMember,
  deleteSSO,
  getSSO,
  inviteMember,
  listInvites,
  listMembers,
  memberSignInLink,
  revokeInvite,
  saveSSO,
  updateMember,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import {
  AuditPanel,
  PoliciesPanel,
  ProvisioningPanel,
  SecretsPanel,
  TelemetryPanel,
} from "./AdminPanels";
import { Modal } from "./Dialogs";

/**
 * The organization's people, for a runtime with members. Everyone sees who their
 * teammates are; owners and admins invite people, hand out sign-in links, change roles,
 * remove and restore, and set up single sign-on. The server holds the rules (an admin
 * cannot touch an owner; there is always an owner) — this only leaves out what the
 * signed-in person's role would be refused.
 *
 * A link appears once, here, as the URL to send. It works once; an invitation for a
 * week, a sign-in link for a day.
 */
type Tab = "people" | "sso" | "policies" | "secrets" | "provisioning" | "telemetry" | "audit";

const TABS: [Tab, string][] = [
  ["people", "People"],
  ["sso", "Single sign-on"],
  ["policies", "Policies"],
  ["secrets", "Secrets"],
  ["provisioning", "Provisioning"],
  ["telemetry", "Telemetry"],
  ["audit", "Audit log"],
];

export function TeamDialog({ me, onClose }: { me: Me; onClose: () => void }) {
  const admin = me.role !== "member";
  const [tab, setTab] = useState<Tab>("people");
  return (
    <Modal title="Team" onClose={onClose}>
      {admin && (
        <div className="seg subtabs team-tabs" style={{ marginBottom: 12 }}>
          {TABS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={cx("seg-btn", tab === key && "on")}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {tab === "people" && <People me={me} />}
      {tab === "sso" && <SSOSettings />}
      {tab === "policies" && <PoliciesPanel />}
      {tab === "secrets" && <SecretsPanel />}
      {tab === "provisioning" && <ProvisioningPanel />}
      {tab === "telemetry" && <TelemetryPanel />}
      {tab === "audit" && <AuditPanel />}
    </Modal>
  );
}

const ROLES: Role[] = ["member", "admin", "owner"];

function People({ me }: { me: Me }) {
  const admin = me.role !== "member";
  const members = useResource(listMembers);
  const inviteFetcher = useCallback(
    (signal: AbortSignal) => (admin ? listInvites(signal) : Promise.resolve({ invites: [] })),
    [admin],
  );
  const invites = useResource(inviteFetcher);
  const [link, setLink] = useState<{ who: string; url: string } | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const invite = useAction(() => inviteMember(email.trim(), role), {
    onDone: (made) => {
      setLink({ who: made.email, url: made.link ?? "" });
      setEmail("");
      invites.refresh();
    },
  });
  const change = useAction(
    (id: string, patch: { role?: Role; active?: boolean }) => updateMember(id, patch),
    { onDone: members.refresh },
  );
  const signIn = useAction((m: TeamMember) => memberSignInLink(m.id), {
    onDone: (made) => setLink({ who: "", url: made.link }),
  });
  const revoke = useAction((id: string) => revokeInvite(id), { onDone: invites.refresh });
  const error = invite.error || change.error || signIn.error || revoke.error || members.error;

  return (
    <div>
      {members.data?.members.map((m) => {
        const self = m.id === me.id;
        const mayChange = admin && !self && (me.role === "owner" || m.role !== "owner");
        return (
          <div key={m.id} className={cx("member-row", !m.active && "off")}>
            <div className="grow">
              <strong>{m.name || m.email}</strong>
              {self && <span className="muted"> (you)</span>}
              <div className="routine-meta">
                {m.email}
                {!m.active && " · removed"}
                {m.last_seen_at && ` · seen ${new Date(m.last_seen_at).toLocaleDateString()}`}
              </div>
            </div>
            {mayChange && m.active ? (
              <select
                className="input"
                aria-label={`Role of ${m.email}`}
                value={m.role}
                disabled={change.pending}
                onChange={(e) => void change.run(m.id, { role: e.target.value as Role })}
              >
                {ROLES.filter((r) => r !== "owner" || me.role === "owner").map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            ) : (
              <span className="muted">{m.role}</span>
            )}
            {mayChange && m.active && (
              <button
                type="button"
                className="pbtn"
                disabled={signIn.pending}
                onClick={() => void signIn.run(m)}
              >
                Sign-in link
              </button>
            )}
            {mayChange && (
              <button
                type="button"
                className={cx("pbtn", m.active && "danger")}
                disabled={change.pending}
                onClick={() => {
                  if (
                    !m.active ||
                    window.confirm(
                      `Remove ${m.name || m.email}? They are signed out everywhere; their bots stay.`,
                    )
                  )
                    void change.run(m.id, { active: !m.active });
                }}
              >
                {m.active ? "Remove" : "Restore"}
              </button>
            )}
          </div>
        );
      })}

      {link && (
        <div className="link-box">
          <div className="routine-meta">
            {link.who ? `Send this to ${link.who}` : "Send this link"} — it works once.
          </div>
          <input
            className="input"
            readOnly
            value={link.url}
            onFocus={(e) => e.currentTarget.select()}
          />
          <button
            type="button"
            className="pbtn"
            onClick={() => void navigator.clipboard?.writeText(link.url).catch(() => undefined)}
          >
            Copy
          </button>
        </div>
      )}

      {admin && (
        <>
          <h3 className="team-h">Invite someone</h3>
          <div className="form">
            <div className="form-row">
              <label>
                Email
                <input
                  type="email"
                  value={email}
                  placeholder="name@company.com"
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label style={{ flex: "0 0 120px" }}>
                Role
                <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
                  {ROLES.filter((r) => r !== "owner" || me.role === "owner").map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="pbtn primary"
                style={{ flex: "0 0 auto" }}
                disabled={invite.pending || !email.includes("@")}
                onClick={() => void invite.run()}
              >
                Invite
              </button>
            </div>
          </div>
          {(invites.data?.invites.length ?? 0) > 0 && (
            <>
              <h3 className="team-h">Waiting to join</h3>
              {invites.data?.invites.map((i) => (
                <div key={i.id} className="member-row">
                  <div className="grow">
                    {i.email}
                    <div className="routine-meta">
                      {i.role} · link expires {new Date(i.expires_at).toLocaleDateString()}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="pbtn"
                    disabled={revoke.pending}
                    onClick={() => void revoke.run(i.id)}
                  >
                    Revoke
                  </button>
                </div>
              ))}
            </>
          )}
        </>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
    </div>
  );
}

function SSOSettings() {
  const sso = useResource(getSSO);
  const [form, setForm] = useState<{
    issuer: string;
    client_id: string;
    client_secret: string;
    domains: string;
    auto_join: boolean;
    enabled: boolean;
  } | null>(null);
  const current = sso.data;
  const draft = form ?? {
    issuer: current?.issuer ?? "",
    client_id: current?.client_id ?? "",
    client_secret: "",
    domains: (current?.domains ?? []).join(", "),
    auto_join: current?.auto_join ?? false,
    enabled: current?.enabled ?? true,
  };
  const set = (patch: Partial<typeof draft>) => setForm({ ...draft, ...patch });
  const save = useAction(
    () =>
      saveSSO({
        issuer: draft.issuer.trim(),
        client_id: draft.client_id.trim(),
        client_secret: draft.client_secret || undefined,
        domains: draft.domains
          .split(/[\s,]+/)
          .map((d) => d.trim())
          .filter(Boolean),
        auto_join: draft.auto_join,
        enabled: draft.enabled,
      }),
    {
      onDone: () => {
        setForm(null);
        sso.refresh();
      },
    },
  );
  const remove = useAction(deleteSSO, {
    onDone: () => {
      setForm(null);
      sso.refresh();
    },
  });
  if (!current) return sso.error ? <ErrorNotice>{sso.error}</ErrorNotice> : null;

  return (
    <div className="form">
      <p className="screen-help" style={{ margin: 0 }}>
        Members sign in through your identity provider (OpenID Connect: Okta, Microsoft Entra,
        Google Workspace, Keycloak…). Register an app there with this redirect URL, then enter its
        issuer and client below.
      </p>
      <label>
        Redirect URL
        <input className="input" readOnly value={current.callback_url} />
      </label>
      <label>
        Issuer
        <input
          value={draft.issuer}
          placeholder="https://your-company.okta.com"
          onChange={(e) => set({ issuer: e.target.value })}
        />
      </label>
      <div className="form-row">
        <label>
          Client ID
          <input value={draft.client_id} onChange={(e) => set({ client_id: e.target.value })} />
        </label>
        <label>
          Client secret
          <input
            type="password"
            autoComplete="off"
            value={draft.client_secret}
            placeholder={current.has_secret ? "saved — leave empty to keep" : ""}
            onChange={(e) => set({ client_secret: e.target.value })}
          />
        </label>
      </div>
      <label>
        <span>
          Email domains <small>— people whose email is in these sign in this way</small>
        </span>
        <input
          value={draft.domains}
          placeholder="company.com"
          onChange={(e) => set({ domains: e.target.value })}
        />
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.auto_join}
          onChange={(e) => set({ auto_join: e.target.checked })}
        />
        <span>Let anyone in these domains join as a member the first time they sign in</span>
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => set({ enabled: e.target.checked })}
        />
        <span>On</span>
      </label>
      {(save.error || remove.error) && <ErrorNotice>{save.error || remove.error}</ErrorNotice>}
      <div className="form-actions">
        {current.configured && (
          <button
            type="button"
            className="pbtn danger"
            disabled={remove.pending}
            onClick={() => {
              if (window.confirm("Turn off single sign-on? Members can still use sign-in links."))
                void remove.run();
            }}
          >
            Remove
          </button>
        )}
        <button
          type="button"
          className="pbtn primary"
          disabled={save.pending || !draft.issuer.trim() || !draft.client_id.trim()}
          onClick={() => void save.run()}
        >
          {save.pending ? "Checking the provider…" : "Save"}
        </button>
      </div>
    </div>
  );
}
