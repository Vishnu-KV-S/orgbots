import Link from "next/link";

/** The bar across the top of every screen. Children are its right-hand side. */
export function TopBar({ children }: { children?: React.ReactNode }) {
  return (
    <header className="topbar">
      <Link href="/" className="brand">
        <span className="dot" />
        agent-org-runtime
      </Link>
      {children}
    </header>
  );
}

/** A breadcrumb segment. `separator` prefixes it with the path slash. */
export function Crumb({
  separator,
  children,
}: {
  separator?: boolean;
  children: React.ReactNode;
}) {
  return (
    <span className="crumb">
      {separator && <span className="sep">/</span>}
      {children}
    </span>
  );
}

/** Pushes everything after it to the right. */
export function Spacer() {
  return <span className="spacer" />;
}

/** The group of buttons at the right of a top bar. */
export function Toolbar({ children }: { children: React.ReactNode }) {
  return <div className="toggles">{children}</div>;
}
