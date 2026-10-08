# ui — the org console

A Next.js canvas over the runtime. It answers the question the four
control-surface endpoints do not — *which companies are running right now, what is
inside them, and what are they doing?* — and, since the control surface landed, it
is also where you answer them.

**It is no longer only a viewer.** It was, and the sentence that used to be here
said so: *"It cannot start, cancel or change anything, by construction."* That
stopped being true when `/v1/control` was added. What is true now:

- `/v1/observe` is still read-only by construction — every statement in
  `runtime/api/observe.py` is a SELECT — and the proxy still forwards `GET` only
  to it.
- `/v1/control` accepts `GET`, `POST`, `PUT` and `DELETE`, and it is the only
  prefix the proxy will forward a write to. Every write goes through the same
  `RunService`, kill switch, spec compiler and spec-root confinement the CLI
  uses, so the console is not a second door around the checks — it is the same
  door with a screen in front of it.

`app/rt/[...path]/route.ts` is where that allowlist lives, and it is the one file
to read if you want to know what this UI can do to a running organization.

> **`/v1/control` has no authentication.** Anything that can reach it can stop a
> department, spend money and rewrite the files under `RUNTIME_SPEC_ROOTS`. This
> is fine on a local devstack and must not be exposed on a network.
> `RUNTIME_SPEC_EDITABLE=false` turns off the file writes; nothing turns off the
> rest.

## Running it

Three processes now, not two. The API:

```bash
# from the repository root, with the devstack up
set -a; . ./.env; set +a
uvicorn runtime.api.app:app --port 8000
```

A worker, which since the conductor landed is what makes the loop turn on its
own — crons fire, inbox messages become runs, and nobody types `tick`:

```bash
python -m runtime.worker.main
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

**`/`** — your bots. Sidebar → conversation → computer/details, with ⌘K search.
See *Bots* in the repository README. Needs the computer process
(`python -m runtime.computer.main`) for the live screen.

**`/companies`** — every organization the runtime knows, with its live-run count, failure
count and open tasks. `running` means at least one run is queued or executing.
`halted` means a kill switch is engaged, which deliberately outranks `running`:
the runs still counted as live in a halted org are the ones draining.

**`/specs`** — the config plane, with an editor.

- A **folder** under `RUNTIME_SPEC_ROOTS` is what compiles into a company; a
  **file** is what you edit. The tree keeps them at different levels because
  planning one file of a corpus is not a thing that means anything.
- The editor is CodeMirror with YAML syntax, folding and a ~20-line linter over
  `parseAllDocuments` — instant feedback for the error you make constantly (a bad
  indent). Whether a document is a *valid spec* is `validate`, on a button,
  because the authoritative validator is server-side and already written.
- **A save writes your bytes.** Comments and key order survive; a save built on
  the exporter would sort every key alphabetically and delete every comment in
  `config/`. It parses first and refuses a file it cannot read, leaving the file
  on disk alone.
- A save is guarded by the digest the file was opened at. If somebody else — the
  CLI, another tab, `git checkout` — changed it meanwhile, you get a 409 rather
  than a silent overwrite. *diff* shows what a save would change before you make
  it.
- **validate → plan → apply**, three buttons, because §13's first risk is a quiet
  bad apply and the guard is reading the diff like a code review. The plan pane
  shows the CLI's own rendering verbatim, its hash, and a countdown to the
  fifteen-minute expiry. A refused rename gets a box to answer it in.

**`/org/<id>`** — the organization as a node graph.

- **Organization → departments → actors**, with actors hung off their manager
  via `reports_to`. An actor with no manager hangs off its department, and one
  with no department off the organization, so nothing floats unattached.
- **Goals and projects** as dashed nodes, with a `project → owner` edge. Toggle
  them off with *goals & projects* when you only want the org chart.
- **Observed delegation** as animated edges, labelled with how many calls
  actually happened. This is traffic that ran, not configuration.
- Each actor node carries its live status, its run tally, its open tasks, its
  spend and its tool grants, so the common questions need no click.

Click any node for the inspector. A **department** panel now shows its derived
state, its members' schedule, and three controls: *start*, *stop…* and *tick
now*. An **actor** panel can start a run, with a mode picked from what that
entrypoint actually dispatches on.

Two things the panels say out loud, because both are invisible otherwise:

- **Stop asks `drain` or `halt`.** `halt` refuses a call *after* its effect has
  fired, leaving an INTENT row for a human to reconcile. It is right when an
  effect in flight is worse than an effect you have to reconcile, and wrong for
  everything else.
- **A stop takes up to ten seconds to reach a worker.** The kill-switch cache is
  per-process and invalidating it clears only the caller's copy.

The **event tail** along the bottom is an SSE stream of every topic in the
organization, resumed from the last id already loaded rather than replayed from
the beginning.

## How the code is arranged

Four layers, each of which may only import from the ones above it:

```
app/          routes. Thin — a route resolves params and renders a screen.
features/     one folder per thing the console does. Owns its data, its
              components, its state. Never imports another feature's internals.
components/   reusable and domain-free. ui/ is the primitive vocabulary
              (Pill, Chip, Card, Section, ListRow…); layout/ is the app frame.
lib/          types, api client, formatting, generic hooks. No JSX except hooks.
```

```
features/
  organizations/   the index screen
  org-graph/       the canvas: nodes/, lib/layout.ts, lib/edges.ts, hooks/
  inspector/       the detail panel: panels/ (one per node kind), runs/,
                   components/ (RunControl)
  events/          the SSE tail
  specs/           the editor: the tree, the editor, the plan pane
```

Two third-party runtime dependencies, and they are the only two:
**`@xyflow/react`** draws the canvas, and **CodeMirror**
(`@uiw/react-codemirror` plus the YAML, lint and merge packages) is the editor.

Monaco was the alternative and was not chosen: it needs `monaco-editor` (~5 MB)
plus `MonacoEnvironment.getWorker` plumbing, which is the known friction point
under Next and Turbopack — and its headline feature, schema-driven completion,
would duplicate a validator that is server-side, authoritative, and already
written.

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
`styles/tokens.css`. There is **one theme, dark** — `tokens.css` is a single
`:root` block; a light theme would add a `[data-theme="light"]` block there and
nothing else would move, including the editor, which names no colours of its own.

## Notes

- The canvas polls every 3s; *pause* stops it. Dragging a node pins it — a poll
  refreshes what a node says, never where you put it. *relayout* undoes that.
- **The specs screen does not poll.** A canvas that refreshes under you is fine;
  an editor that does it eats what you were typing. It re-reads on demand — after
  a save, and after an apply.
- Reads go through `lib/hooks/useResource.ts`; writes go through
  `lib/hooks/useAction.ts`. They have deliberately different rules: a failed poll
  keeps the last good data on screen, and a failed action is loud, stays until
  it is dismissed, and is never retried automatically. A write nobody asked for
  twice is a second department stopped.
- After an action the panels call `onRefresh` rather than patching state. An
  optimistic update would be a second answer to what the organization is, and the
  next poll would contradict it.
- Layout is deterministic (a tidy top-down tree, not a force simulation), so the
  same organization looks the same on every load. See
  `features/org-graph/lib/layout.ts` for why each node keeps exactly one primary
  parent.
- `lib/types.ts` is hand-written against `runtime/api/observe.py` **and**
  `runtime/api/control.py`. If you change the endpoint payloads, change both.
