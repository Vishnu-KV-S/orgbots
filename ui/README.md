# ui — the org viewer

A Next.js canvas over the runtime's read-only observation API. It answers the
question the four control-surface endpoints do not: *which companies are running
right now, what is inside them, and what are they doing?*

It is a **viewer**. It cannot start, cancel or change anything, by construction:
the proxy in `app/rt/[...path]/route.ts` forwards `GET` only, and only to
`/v1/observe/*`. A viewer that could start a run would be reaching around the
admission, authority and budget checks that `RunService` owns.

## Running it

Two processes. The API first:

```bash
# from the repository root, with the devstack up
set -a; . ./.env; set +a
uvicorn runtime.api.app:app --port 8000
```

Then the UI:

```bash
cd ui
npm install       # first time only
npm run dev       # http://localhost:3000
```

If the API is somewhere else, point the UI at it — this is read by the proxy
route on the server, so it is never exposed to the browser:

```bash
RUNTIME_API_URL=http://127.0.0.1:8010 npm run dev
```

`npm run build && npm start` for the production build.

## What you get

**`/`** — every organization the runtime knows, with its live-run count, failure
count and open tasks. `running` means at least one run is queued or executing.
`halted` means a kill switch is engaged, which deliberately outranks `running`:
the runs still counted as live in a halted org are the ones draining.

**`/org/<id>`** — the organization as a node graph.

- **Organization → departments → actors**, with actors hung off their manager
  via `reports_to`. An actor with no manager hangs off its department, and one
  with no department off the organization, so nothing floats unattached.
- **Goals and projects** as dashed nodes, with a `project → owner` edge. Toggle
  them off with *goals & projects* when you only want the org chart.
- **Observed delegation** as animated green edges, labelled with how many calls
  actually happened. This is traffic that ran, not configuration — it appears
  only once a parent run has delegated to a child.
- Each actor node carries its live status, its run tally, its open tasks, its
  spend and its tool grants, so the common questions need no click.

Click any node for the inspector: ceilings, model profiles per call site, cron
triggers, version history, and recent runs. Click a run to drill into its
events, effects, artifacts and output — including the runs it delegated to.

The **event tail** along the bottom is an SSE stream of every topic in the
organization, resumed from the last id already loaded rather than replayed from
the beginning.

## How the code is arranged

Four layers, each of which may only import from the ones above it:

```
app/          routes. Thin — a route resolves params and renders a screen.
features/     one folder per thing the viewer does. Owns its data, its
              components, its state. Never imports another feature's internals.
components/   reusable and domain-free. ui/ is the primitive vocabulary
              (Pill, Chip, Card, Section, ListRow…); layout/ is the app frame.
lib/          types, api client, formatting, generic hooks. No JSX except hooks.
```

```
features/
  organizations/   the index screen
  org-graph/       the canvas: nodes/, lib/layout.ts, lib/edges.ts, hooks/
  inspector/       the detail panel: panels/ (one per node kind), runs/
  events/          the SSE tail
```

The two registries are what keep this from ossifying:

- **`features/org-graph/nodes/registry.ts`** — node kind → component, width,
  minimap colour. Nothing else in the canvas knows the list of kinds.
- **`features/inspector/panels/registry.ts`** — node kind → detail panel.

So **adding a node kind** is: a type in `lib/types.ts`, a component in
`nodes/`, a panel in `inspector/panels/`, one line in each registry, and one
`.node.<kind>` rule in `styles/canvas.css`. No `switch` statement changes, and
nothing else has to learn the kind exists. Adding an **edge kind** is one entry
in `lib/edges.ts` — the legend builds itself from that table.

CSS is global but not free-form: the class names belong to the components in
`components/ui` and are written nowhere else, so a caller says
`<Chip tone="warn">` rather than `className="chip warn"`. `app/globals.css` is
only an import list; the layers live in `styles/`, with every colour a token in
`styles/tokens.css`. That is what makes it possible to move a layer onto CSS
modules, or restyle the app, one file at a time.

## Notes

- The canvas polls every 3s; *pause* stops it. Dragging a node pins it — a poll
  refreshes what a node says, never where you put it. *relayout* undoes that.
- Every screen fetches through `lib/hooks/useResource.ts`, which owns the rules
  that should not differ between screens: an abort is not an error, and a failed
  poll keeps the last good data on screen rather than blanking it.
- Layout is deterministic (a tidy top-down tree, not a force simulation), so the
  same organization looks the same on every load. See
  `features/org-graph/lib/layout.ts` for why each node keeps exactly one primary
  parent.
- `lib/types.ts` is hand-written against `runtime/api/observe.py`. If you change
  the endpoint payloads, change both.
