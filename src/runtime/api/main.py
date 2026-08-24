"""Entrypoints.

`python -m runtime.api.main` runs the API. `python -m runtime.worker.main` runs a
worker plus the relay and reaper. They are separate processes because they scale
on different axes and fail for different reasons — an API that shares a process
with the worker pool goes down whenever a graph does.
"""

from __future__ import annotations

import uvicorn

from runtime.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "runtime.api.app:app",
        host="0.0.0.0",  # noqa: S104  container-facing; bind is controlled by the network
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
