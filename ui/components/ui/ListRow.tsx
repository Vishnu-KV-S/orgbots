import { cx } from "@/lib/cx";

/**
 * A row in a panel list: a title, something on the right, a line of metadata.
 *
 * It renders as a `<button>` when it is clickable and a `<div>` when it is not,
 * so a row that opens a run is reachable from the keyboard and a row that is
 * only information is not a fake control.
 */

export function List({ children }: { children: React.ReactNode }) {
  return <div className="list">{children}</div>;
}

export interface ListRowProps {
  title: React.ReactNode;
  /** Rendered at the far end of the title line — usually a tag or a timestamp. */
  aside?: React.ReactNode;
  meta?: React.ReactNode;
  /** A second metadata line that is allowed to wrap: reasons, errors, URIs. */
  note?: React.ReactNode;
  onClick?: () => void;
}

export function ListRow({ title, aside, meta, note, onClick }: ListRowProps) {
  const body = (
    <>
      <div className="row-top">
        <span className="row-title">{title}</span>
        {aside}
      </div>
      {meta !== undefined && <div className="row-meta">{meta}</div>}
      {note !== undefined && <div className="row-meta wrap">{note}</div>}
    </>
  );

  if (!onClick) return <div className="row">{body}</div>;
  return (
    <button type="button" className={cx("row", "clickable")} onClick={onClick}>
      {body}
    </button>
  );
}
