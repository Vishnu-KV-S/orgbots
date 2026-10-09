# Contributing

Thanks for taking an interest. Issues, ideas and pull requests are all welcome.

## Setting up

Follow the [Quick start](README.md#quick-start). Without Docker,
`eval "$(scripts/devstack.sh up)"` starts a private Postgres and Redis under
`.devstack/`, with no sudo and no system services touched.

## Before you open a pull request

```bash
uv run ruff check .          # lint
uv run lint-imports          # the layer rules in .importlinter
uv run mypy                  # strict typing
uv run pytest -q -m "not slow"
```

- Read the invariants in `ARCHITECTURE.md` §2 first. The pull request template asks
  which ones a change touches, and which test still holds each of them.
- Keep a change to one idea. A bug fix and a refactor are two pull requests.
- New behaviour needs a test that fails without it.
- Never commit secrets. `.env` is gitignored; credentials belong in the vault (see
  *Operating it* in the README).

## Reporting a security problem

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
