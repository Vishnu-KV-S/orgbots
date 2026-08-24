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

export function CardHead({ title, aside }: { title: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="card-head">
      <span className="card-title">{title}</span>
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

/** A labelled number. `warn` is for a count that should not be greater than zero. */
export function Stat({ label, value, warn }: { label: string; value: React.ReactNode; warn?: boolean }) {
  return (
    <div className="stat">
      <div className="k">{label}</div>
      <div className={cx("v", warn && "warn")}>{value}</div>
    </div>
  );
}

export function StatRow({ children }: { children: React.ReactNode }) {
  return <div className="stats">{children}</div>;
}
