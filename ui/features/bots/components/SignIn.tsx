"use client";

import { useEffect, useState } from "react";
import { ErrorNotice } from "@/components/ui";
import { type Role, acceptLink, authMe, linkInfo, startSSO } from "@/lib/api/bots";

/**
 * Signing in, when the runtime has members (`RUNTIME_AUTH_MODE=members`).
 *
 * A link from an admin (or the CLI) opens this page with `?link=`: an invitation to
 * join, or a sign-in for someone who already has. Otherwise the person gives their
 * work email and, if their organization set up single sign-on for its domain, goes to
 * their identity provider and comes back signed in. `?error=` is why the last attempt
 * failed, and `?return_to=` is where to go after (a path on this site only).
 */
export function SignIn() {
  const [params, setParams] = useState<URLSearchParams | null>(null);
  useEffect(() => {
    const now = new URLSearchParams(window.location.search);
    setParams(now);
    if (!now.get("link")) {
      void authMe().then(
        (me) => {
          if (me.mode === "none" || me.member) window.location.replace(returnTo(now));
        },
        () => undefined,
      );
    }
  }, []);
  if (!params) return null;
  const link = params.get("link");
  return (
    <main className="signin">
      <div className="signin-card">
        <h1>Agent Org</h1>
        {link ? <AcceptLink token={link} after={returnTo(params)} /> : <SSO params={params} />}
      </div>
    </main>
  );
}

function returnTo(params: URLSearchParams): string {
  const value = params.get("return_to") ?? "/";
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

const ROLE: Record<Role, string> = { owner: "an owner", admin: "an admin", member: "a member" };

function AcceptLink({ token, after }: { token: string; after: string }) {
  const [info, setInfo] = useState<Awaited<ReturnType<typeof linkInfo>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    linkInfo(token).then(setInfo, (cause: Error) => setError(cause.message));
  }, [token]);

  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await acceptLink(token, name.trim());
      window.location.replace(after);
    } catch (cause) {
      setError((cause as Error).message);
      setBusy(false);
    }
  };

  if (error && !info) {
    return (
      <>
        <ErrorNotice>{error}</ErrorNotice>
        <p className="screen-help">
          Ask whoever sent it for a new link, or <a href="/signin">sign in another way</a>.
        </p>
      </>
    );
  }
  if (!info) return <p className="screen-help">Checking your link…</p>;
  return (
    <div className="form">
      <p className="signin-lead">
        {info.joining ? (
          <>
            You&apos;re invited to join <strong>{info.organization || "the organization"}</strong>{" "}
            as {ROLE[info.role]}.
          </>
        ) : (
          <>Sign in to {info.organization || "the organization"}.</>
        )}
        <br />
        <span className="muted">{info.email}</span>
      </p>
      {info.joining && (
        <label>
          Your name <small>— shown to teammates on what you say to shared bots</small>
          <input
            autoFocus
            value={name}
            maxLength={80}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void go()}
          />
        </label>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
      <button
        type="button"
        className="pbtn primary"
        disabled={busy}
        autoFocus={!info.joining}
        onClick={() => void go()}
      >
        {busy ? "Signing in…" : info.joining ? "Join" : "Sign in"}
      </button>
    </div>
  );
}

function SSO({ params }: { params: URLSearchParams }) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(params.get("error"));
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      const { redirect_url } = await startSSO(email.trim(), returnTo(params));
      window.location.assign(redirect_url);
    } catch (cause) {
      setError((cause as Error).message);
      setBusy(false);
    }
  };
  return (
    <div className="form">
      <p className="signin-lead">Sign in with your work account.</p>
      <label>
        Work email
        <input
          type="email"
          autoFocus
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && email.includes("@") && void go()}
        />
      </label>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      <button
        type="button"
        className="pbtn primary"
        disabled={busy || !email.includes("@")}
        onClick={() => void go()}
      >
        {busy ? "Going to your sign-in page…" : "Continue"}
      </button>
      <p className="screen-help">
        No single sign-on? An admin can send you a sign-in link from the Team page.
      </p>
    </div>
  );
}
