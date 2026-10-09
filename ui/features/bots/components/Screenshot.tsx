"use client";

import { useEffect, useState } from "react";
import { savedScreenshotUrl } from "@/lib/api/bots";
import { cx } from "@/lib/cx";

/**
 * A picture of the bot's screen, kept by the run at a moment that mattered — an
 * approval, a sign-in, a CAPTCHA, a look, or a reply the bot chose to illustrate.
 *
 * Masked like everything a bot sees: password, code and saved-login fields are grey
 * boxes. A thumbnail in the conversation; click for the full size. Pictures age out
 * (each bot keeps its most recent ones), so a missing one says so instead of
 * leaving a broken image.
 */
export function Screenshot({
  botId,
  screenshotId,
  caption,
  size = "md",
}: {
  botId: string;
  screenshotId: string;
  caption?: string;
  size?: "sm" | "md";
}) {
  const [open, setOpen] = useState(false);
  const [gone, setGone] = useState(false);
  const src = savedScreenshotUrl(botId, screenshotId);
  const alt = caption ? `Bot's screen: ${caption}` : "Screenshot of the bot's screen";

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (gone) return <div className="shot-gone">Screenshot no longer kept</div>;

  return (
    <>
      <button
        type="button"
        className={cx("shot", size === "sm" && "sm")}
        onClick={() => setOpen(true)}
        title="Open full size"
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- an API image, not a static asset */}
        <img src={src} alt={alt} loading="lazy" onError={() => setGone(true)} />
      </button>
      {open && (
        <div
          className="scrim shot-scrim"
          role="dialog"
          aria-modal
          aria-label={alt}
          onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}
        >
          <figure className="shot-full">
            {/* eslint-disable-next-line @next/next/no-img-element -- see above */}
            <img src={src} alt={alt} />
            <figcaption>
              {caption && <span>{caption}</span>}
              <button type="button" className="pbtn" onClick={() => setOpen(false)}>
                Close
              </button>
            </figcaption>
          </figure>
        </div>
      )}
    </>
  );
}
