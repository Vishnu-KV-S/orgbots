/**
 * The three things a screen says when it has nothing to show: nothing here,
 * still loading, or the runtime is not answering. Uniform wording and uniform
 * styling because these are the states a viewer sees most often when something
 * is wrong, and inconsistency there reads as breakage.
 */

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Loading({ what }: { what?: string }) {
  return <p className="empty">Loading{what ? ` ${what}` : ""}…</p>;
}

export function ErrorNotice({ children }: { children: React.ReactNode }) {
  return (
    <div className="error" role="alert">
      {children}
    </div>
  );
}

/** The one error every screen can hit: the runtime API is not up. */
export function ApiUnreachable({ detail }: { detail: string }) {
  return (
    <ErrorNotice>
      Could not reach the runtime API — <code>{detail}</code>
      <br />
      Start it with <code>uvicorn runtime.api.app:app --port 8000</code>, or point the UI
      elsewhere with <code>RUNTIME_API_URL</code>.
    </ErrorNotice>
  );
}

/** Pretty-printed JSON, for run output and anything else free-form. */
export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
