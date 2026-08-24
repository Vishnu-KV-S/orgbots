import { cx } from "@/lib/cx";

/**
 * The three badge shapes this UI uses, as components rather than class strings.
 *
 * They are deliberately dumb: they know nothing about runs, actors or tasks. A
 * caller passes a *tone* or a *status token*, never a colour — so restyling the
 * whole app, or moving it onto CSS modules or a utility framework, is a change
 * to these files and the stylesheet, not to forty call sites.
 */

export type Tone = "neutral" | "ok" | "bad" | "warn";

/** A status dot with a word next to it: running / queued / idle / halted. */
export type PillStatus = "running" | "queued" | "idle" | "halted" | "failed";

export function Pill({ status, children }: { status: PillStatus | string; children?: React.ReactNode }) {
  return <span className={cx("pill", status)}>{children ?? status}</span>;
}

/** A small square-ish label for a count, a grant, a price. */
export function Chip({
  tone = "neutral",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className={cx("chip", tone !== "neutral" && tone)} title={title}>
      {children}
    </span>
  );
}

/**
 * A run/task lifecycle token straight off the wire (`SUCCESS`, `REWORK_REQUIRED`
 * …). The stylesheet maps the tokens it knows to a colour and lets the rest fall
 * through as neutral, so a new status added to the runtime shows up as text
 * rather than as a crash or a blank.
 */
export function StatusTag({ status, children }: { status: string; children?: React.ReactNode }) {
  return <span className={cx("tag", status)}>{children ?? status}</span>;
}

/** A horizontal run of chips/pills — the standard way to group them. */
export function BadgeRow({ children }: { children: React.ReactNode }) {
  return <div className="node-row">{children}</div>;
}
