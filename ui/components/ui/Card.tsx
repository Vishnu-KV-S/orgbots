import Link from "next/link";
import { cx } from "@/lib/cx";

/** A clickable summary tile. `href` makes it a link; without one it is static. */
export function Card({
  href,
  children,
}: {
  href?: string;
  children: React.ReactNode;
}) {
  if (href) {
    return (
      <Link href={href} className="card">
        {children}
      </Link>
    );
  }
  return <div className="card">{children}</div>;
}

export function CardGrid({ children }: { children: React.ReactNode }) {
  return <div className="cards">{children}</div>;
}

/**
 * The tile's heading. `sub` is the quiet line under the name — the facts that
 * describe what the thing *is*, kept out of the stat row so that row only ever
 * carries what is happening right now.
 */
export function CardHead({
  title,
  sub,
  aside,
}: {
  title: React.ReactNode;
  sub?: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <div className="card-head">
      <span className="card-heading">
        <span className="card-title">{title}</span>
        {sub !== undefined && <span className="card-sub">{sub}</span>}
      </span>
      {aside}
    </div>
  );
}

export function CardFoot({ left, right }: { left?: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="card-foot">
      <span>{left}</span>
      <span>{right}</span>
    </div>
  );
}

/**
 * A labelled number. `warn` is for a count that should not be greater than
 * zero; `emphasis` marks the one number on the tile worth reading first.
 *
 * A zero recedes on its own. Four counts at equal weight is four things to
 * read; dimming the ones that are nothing to report leaves the tile saying
 * only what is true of it, which is what makes a grid of these scannable.
 */
export function Stat({
  label,
  value,
  warn,
  emphasis,
}: {
  label: string;
  value: React.ReactNode;
  warn?: boolean;
  emphasis?: boolean;
}) {
  const zero = value === 0 || value === "0";
  return (
    <div className="stat">
      <div className="k">{label}</div>
      <div className={cx("v", zero && "zero", warn && "warn", emphasis && !zero && "live")}>
        {value}
      </div>
    </div>
  );
}

export function StatRow({ children }: { children: React.ReactNode }) {
  return <div className="stats">{children}</div>;
}
