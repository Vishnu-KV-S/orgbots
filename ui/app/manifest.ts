import type { MetadataRoute } from "next";

/**
 * The web app manifest: what makes "Install app" appear in Chrome and Edge, and "Add to
 * Home Screen" open full-screen on a phone. The installed app is the same UI, in its own
 * window, and the one push notifications (`public/sw.js`) reach when it is closed.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Agent Org — your AI employees",
    short_name: "Bots",
    description: "Persistent AI employees: chat, delegate, approve and watch them work.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#0a0a0a",
    theme_color: "#0a0a0a",
    icons: [
      { src: "/icon-192x192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512x512.png", sizes: "512x512", type: "image/png" },
      { src: "/icon-512x512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
