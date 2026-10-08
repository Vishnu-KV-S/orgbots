"""`python -m runtime.computer.main` — start the shared browser.

RUNTIME_COMPUTER_PORT=8020            # what RUNTIME_COMPUTER_URL points at
RUNTIME_COMPUTER_PROFILE=.computer    # cookies and logins live here
RUNTIME_COMPUTER_HEADLESS=true
COMPUTER_CHROMIUM_PATH=...            # optional: a specific Chromium build
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from runtime.computer.app import create_app


def main() -> None:
    port = int(os.environ.get("RUNTIME_COMPUTER_PORT", "8020"))
    host = os.environ.get("RUNTIME_COMPUTER_HOST", "127.0.0.1")
    profile = Path(os.environ.get("RUNTIME_COMPUTER_PROFILE", ".computer/profile"))
    headless = os.environ.get("RUNTIME_COMPUTER_HEADLESS", "true").lower() != "false"
    uvicorn.run(create_app(profile, headless=headless), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
