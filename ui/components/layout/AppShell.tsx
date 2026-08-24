/**
 * The full-height column every screen lives in: a top bar, then the screen,
 * then whatever the screen wants pinned to the bottom (the event tail).
 */
export function AppShell({
  bar,
  footer,
  children,
}: {
  bar: React.ReactNode;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="shell">
      {bar}
      {children}
      {footer}
    </div>
  );
}

/** A scrolling, centred content column — the list screens use it. */
export function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="page">
      <div className="page-inner">{children}</div>
    </div>
  );
}
