import type { Metadata } from "next";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Org Runtime",
  description: "Live view of the organizations running on the agent runtime",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // `suppressHydrationWarning`: browser extensions stamp attributes onto <html>
  // before React loads — an ad blocker's opt-out flag, a theme extension's class.
  // The mismatch is on this one element and there is nothing to reconcile.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
