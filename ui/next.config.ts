import type { NextConfig } from "next";

const config: NextConfig = {
  // The runtime API is reached through `app/rt/[...path]/route.ts` rather than
  // through a rewrite, because one of the endpoints is an SSE tail and a route
  // handler is where we can hand the upstream body straight back as a stream.
  reactStrictMode: true,
  // `next build` also writes a self-contained server (`.next/standalone/server.js`),
  // which is what the single-container image runs. `next dev` is unaffected.
  output: "standalone",
};

export default config;
