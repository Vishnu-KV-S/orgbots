import { cx } from "@/lib/cx";

/** A toolbar button. `pressed` makes it a toggle and sets `aria-pressed`. */
export function Button({
  pressed,
  onClick,
  title,
  children,
}: {
  pressed?: boolean;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={cx("btn", pressed && "on")}
      aria-pressed={pressed === undefined ? undefined : pressed}
      onClick={onClick}
      title={title}
    >
      {children}
    </button>
  );
}

/**
 * Text that acts like a link but goes nowhere the router knows about — it
 * selects a node on the canvas. A real `<a>` here would offer a middle-click
 * that opens nothing.
 */
export function InlineButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" className="linklike" onClick={onClick}>
      {children}
    </button>
  );
}

/** An icon-sized button for panel affordances: close, back. */
export function IconButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button type="button" className="close" onClick={onClick} title={label} aria-label={label}>
      {children}
    </button>
  );
}
