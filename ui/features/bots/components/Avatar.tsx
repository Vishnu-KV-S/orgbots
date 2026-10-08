import { cx } from "@/lib/cx";
import { initials } from "../lib/text";

/** A bot's face: its emoji if it has one, its initials otherwise. */
export function Avatar({
  name,
  avatar,
  working,
  size,
}: {
  name: string;
  avatar?: string;
  working?: boolean;
  size?: "sm" | "lg";
}) {
  return (
    <span className={cx("avatar", size)} aria-hidden>
      {avatar || initials(name)}
      {working && <span className="working" title="Working" />}
    </span>
  );
}
