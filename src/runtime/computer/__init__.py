"""The computer — one shared browser that every bot drives.

A separate process, for the same reason the API and the worker are separate: it fails
for different reasons (a page that hangs the renderer, a browser that runs out of
memory) and it must outlive any one run. Bots reach it only through the
`browser.observe@1` / `browser.act@1` tools in `runtime.gateway.builtin.browser`, so
every action a bot takes still passes the gateway's fence, kill switch, ceilings,
journal and audit. The UI reaches it through `/v1/bots/.../computer`, which is how a
person watches a screen and takes control of it.

**One browser, one profile, one screen per bot.** All bots share cookies and logins —
the GrokBot model, where a person signs in once and every bot can use the session —
but each bot has its own page, so two bots never fight over one tab. Nothing here
imports the rest of the runtime: it is an external system the gateway calls, like a
search API, and it has no database access to defeat a fence with.
"""
