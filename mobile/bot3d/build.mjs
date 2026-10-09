// Bundles src/embed.tsx — the web app's own 3D bot code — into one self-contained HTML
// file the Flutter app ships as an asset: ../assets/bot3d/index.html.
//
//   cd mobile/bot3d && npm ci && npm run build      (needs `npm ci` in ui/ first)
//
// three.js, React and React Three Fiber resolve from ui/node_modules, so the phone draws
// bots with exactly the versions the web app pins. CI rebuilds this file and fails if the
// committed one is stale.

import { build } from "esbuild";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const ui = resolve(here, "../../ui");
const out = resolve(here, "../assets/bot3d/index.html");

const result = await build({
  entryPoints: [resolve(here, "src/embed.tsx")],
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020", "safari15", "chrome100"],
  jsx: "automatic",
  write: false,
  legalComments: "none",
  nodePaths: [resolve(ui, "node_modules")],
  alias: { "@": ui },
  define: { "process.env.NODE_ENV": '"production"' },
  logLevel: "warning",
});

const js = result.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
const html = `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Orgbots bot</title>
<style>html,body,#root{margin:0;height:100%;background:transparent;overflow:hidden;-webkit-user-select:none;user-select:none}</style>
</head>
<body>
<div id="root"></div>
<script>${js}</script>
</body>
</html>
`;
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, html);
console.log(`wrote ${out} (${(html.length / 1024).toFixed(0)} KB)`);
