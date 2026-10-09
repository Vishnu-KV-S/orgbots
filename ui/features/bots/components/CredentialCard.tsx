"use client";

import { type FormEvent, useRef, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import {
  type BotMessage,
  type CredentialField,
  type CredentialKind,
  cancelCredentials,
  submitCredentials,
  chooseSavedLogin,
} from "@/lib/api/bots";
import { cx } from "@/lib/cx";
import { Screenshot } from "./Screenshot";

const TITLE = {
  sign_in: "Sign in to",
  sign_up: "Create an account on",
  verify: "Verification code for",
} as const;

const SUBMIT = { sign_in: "Sign in", sign_up: "Create account", verify: "Verify" } as const;

/** How each kind is typed in *this* form, so a password manager can fill the card too. */
const INPUT: Record<
  CredentialKind,
  { type: string; autoComplete: string; inputMode?: "numeric" | "email" | "tel" }
> = {
  email: { type: "email", autoComplete: "username", inputMode: "email" },
  username: { type: "text", autoComplete: "username" },
  phone: { type: "tel", autoComplete: "tel", inputMode: "tel" },
  password: { type: "password", autoComplete: "current-password" },
  new_password: { type: "password", autoComplete: "new-password" },
  confirm_password: { type: "password", autoComplete: "new-password" },
  otp: { type: "text", autoComplete: "one-time-code", inputMode: "numeric" },
  name: { type: "text", autoComplete: "name" },
  text: { type: "text", autoComplete: "off" },
};

const SECRET = new Set<CredentialKind>(["password", "new_password", "confirm_password"]);

/**
 * The bot reached a sign-in, sign-up or code page and needs the person.
 *
 * The fields are the ones the runtime read off the page, not the bot's description of
 * them. What is typed here goes in one request to the vault, and from there into the
 * browser: the bot is told the form was filled, never with what. The inputs are
 * uncontrolled — their values are read once, on submit, and the form is cleared —
 * so nothing typed here lives on in React state.
 */
export function CredentialCard({
  botId,
  botName,
  message,
  live,
  decision,
  onDone,
  shared = false,
}: {
  botId: string;
  botName: string;
  message: BotMessage;
  live: boolean;
  decision: { kind: string; saved: boolean } | null;
  onDone: () => void;
  /** A team bot: a saved login is its own, used whoever talks to it. */
  shared?: boolean;
}) {
  const p = message.payload;
  const requestId = p.credential_request_id ?? "";
  const purpose = p.purpose ?? "sign_in";
  const host = p.host ?? "";
  const fields: CredentialField[] = p.fields ?? [];
  const saved = Array.isArray(p.saved) ? p.saved : [];
  const keepable = fields.some((f) => SECRET.has(f.kind));
  const form = useRef<HTMLFormElement>(null);
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const finish = async (call: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await call();
      form.current?.reset();
      onDone();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const values: Record<string, string> = {};
    for (const f of fields) values[f.key] = String(data.get(f.key) ?? "");
    const save = keepable && data.get("__save") === "on";
    void finish(() => submitCredentials(botId, requestId, values, save));
  };

  return (
    <div className={cx("approval", "creds", !live && "decided")}>
      <div className="approval-title">
        {live ? <span className="badge-wait">Needs you</span> : null}
        <span>
          {TITLE[purpose]} {host}
        </span>
      </div>
      {message.content && <div className="reason">{botName}: {message.content}</div>}
      {p.screenshot_id && (
        <Screenshot botId={botId} screenshotId={p.screenshot_id} caption={`The page on ${host}`} size="sm" />
      )}
      {live && p.retry && (
        <div className="reason creds-warn">
          The last attempt didn&apos;t get past this form. The details may be wrong.
        </div>
      )}

      {live ? (
        <>
          {saved.length > 0 && (
            <div className="creds-saved">
              {saved.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className="pbtn"
                  disabled={busy}
                  onClick={() => void finish(() => chooseSavedLogin(botId, requestId, s.id))}
                >
                  Use saved login {s.label}
                </button>
              ))}
              <span className="creds-or">or enter details:</span>
            </div>
          )}
          <form ref={form} className="creds-form" onSubmit={submit} autoComplete="on">
            {fields.map((f, i) => {
              const input = INPUT[f.kind] ?? INPUT.text;
              const secret = SECRET.has(f.kind);
              return (
                <label key={f.key} className="creds-field">
                  <span>{f.label}</span>
                  <span className="creds-input">
                    <input
                      name={f.key}
                      type={secret && shown[f.key] ? "text" : input.type}
                      autoComplete={input.autoComplete}
                      inputMode={input.inputMode}
                      required={f.kind !== "text"}
                      maxLength={512}
                      spellCheck={false}
                      autoCapitalize="off"
                      autoCorrect="off"
                      autoFocus={i === 0}
                      disabled={busy}
                    />
                    {secret && (
                      <button
                        type="button"
                        className="ibtn"
                        aria-label={shown[f.key] ? "Hide" : "Show"}
                        onClick={() => setShown((s) => ({ ...s, [f.key]: !s[f.key] }))}
                      >
                        {shown[f.key] ? "Hide" : "Show"}
                      </button>
                    )}
                  </span>
                </label>
              );
            })}
            {keepable && (
              <label className="creds-keep">
                <input type="checkbox" name="__save" defaultChecked={!shared} disabled={busy} />
                {shared
                  ? `Save it as ${botName}'s login — used for everyone on your team who talks to it`
                  : "Save to the vault for next time"}
              </label>
            )}
            <div className="creds-note">
              🔒 Goes straight into the browser from an encrypted vault. {botName} never sees
              what you type.
            </div>
            {error && <ErrorNotice>{error}</ErrorNotice>}
            <div className="approval-actions">
              <button type="submit" className="pbtn primary" disabled={busy}>
                {SUBMIT[purpose]}
              </button>
              <button
                type="button"
                className="pbtn"
                disabled={busy}
                onClick={() => void finish(() => cancelCredentials(botId, requestId))}
              >
                Not now
              </button>
            </div>
          </form>
        </>
      ) : (
        <div className="reason">{decided(decision)}</div>
      )}
    </div>
  );
}

function decided(decision: { kind: string; saved: boolean } | null): string {
  if (!decision) return "No longer waiting";
  if (decision.kind === "submitted")
    return decision.saved ? "Entered securely · saved to the vault" : "Entered securely";
  if (decision.kind === "saved") return "Used a saved login";
  if (decision.kind === "cancelled") return "Declined";
  return "No longer waiting";
}
