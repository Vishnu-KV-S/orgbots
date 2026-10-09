import { ArrowLeftIcon } from "@heroicons/react/24/outline";
import Link from "next/link";

/**
 * The bar across the top of every company-console screen. Children are its
 * right-hand side. The brand goes to the company list; the bots workspace is the
 * home screen and has its own sidebar instead of this bar.
 */
export function TopBar({ children }: { children?: React.ReactNode }) {
  return (
    <header className="topbar">
      <Link href="/" className="crumb back-to-bots" title="Back to your bots">
        <ArrowLeftIcon /> Bots
      </Link>
      <Link href="/companies" className="brand">
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
