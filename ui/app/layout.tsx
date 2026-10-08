import type { Metadata } from "next";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Org",
  description: "Persistent AI employees and the organizations they work in",
};

/**
 * Runs while the HTML is parsed, before first paint, so a light-theme user never
 * sees a flash of the dark one. `system` (the default) follows the OS. The value is
 * written by the Settings dialog; reading it can throw in a locked-down browser, in
 * which case the dark default stands.
 */
const THEME_SCRIPT = `try{var t=localStorage.getItem("theme")||"system";if(t==="system"){t=matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"}document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // `suppressHydrationWarning`: browser extensions stamp attributes onto <html>
  // before React loads — an ad blocker's opt-out flag, a theme extension's class —
  // and the theme script above sets `data-theme`. The DOM wins on this one element.
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
