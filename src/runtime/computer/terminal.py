"""The computer's terminal and its workspace.

Every bot has a shell on the shared computer, and every bot shares one directory,
`/workspace`: what one bot downloads or builds there, the next can use — the way GrokBot
describes its cloud computer. Browser downloads land in `/workspace/downloads`.

**A command runs in a sandbox** (bubblewrap): the system's programs read-only, the
workspace as the only writable directory besides a private `/tmp`, no view of the home
directory, the browser profile, the runtime's checkout or its `.env`, and an environment
with nothing in it but a PATH. The network is on — installing a package, fetching a
page — unless `COMPUTER_SANDBOX_NETWORK=off`. Every command has a timeout and its output
is cut at `OUTPUT_BYTES`; the whole process group is killed on timeout, so a background
child does not outlive it.

**A local command runs on the person's own machine** (`local`): no sandbox, in the
workspace directory, as the person's user — for the jobs a sandbox cannot do. The bot
asks before every one unless the person chose "always allow" for that bot
(`domain.bots.needs_approval`), and `COMPUTER_LOCAL_COMMANDS=off` turns it off for
everyone. The environment is still scrubbed: the computer is often started with the
runtime's `.env` exported, and a local command is not a way to read its keys.

Paths are confined to the workspace after symlinks are resolved, so neither a bot nor
a link it made can reach outside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTPUT_BYTES = 64 * 1024
"""Per stream. A build log longer than this is cut, with a note saying so."""

MAX_TIMEOUT_S = 300.0
DEFAULT_TIMEOUT_S = 60.0
MAX_FILE_BYTES = 10 * 1024 * 1024
LIST_ENTRIES = 500

_SAFE_ENV = ("LANG", "LC_ALL", "TZ", "TERM")
_RO_ETC = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/alternatives",
    "/etc/localtime",
    "/etc/passwd",
    "/etc/group",
    "/etc/ld.so.cache",
    "/etc/python3",
    "/etc/gitconfig",
)


class TerminalError(Exception):
    """A command or a path that cannot be run or used. The message is shown to the bot."""


def workspace_for(base: Path, profile: str) -> Path:
    """A browser profile's workspace: the base for `""`, a sibling directory for any
    other — never inside the base, where the default profile's bots could read it."""
    return base if not profile else base.with_name(f"{base.name}-{profile}")


@dataclass
class Ran:
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    seconds: float
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "seconds": round(self.seconds, 2),
            "mode": self.mode,
        }


class Terminal:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "downloads").mkdir(exist_ok=True)
        self._root = self.workspace.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self.bwrap = shutil.which("bwrap")
        self.network = os.environ.get("COMPUTER_SANDBOX_NETWORK", "on").lower() != "off"
        self.local_allowed = os.environ.get("COMPUTER_LOCAL_COMMANDS", "on").lower() != "off"

    # --- commands ----------------------------------------------------------------------

    async def run(
        self,
        screen_id: str,
        command: str,
        *,
        timeout_s: float,
        local: bool = False,
        network: bool = True,
        secrets: dict[str, str] | None = None,
    ) -> Ran:
        """Run one command for one bot. A bot's commands run one at a time.

        `network` False runs the sandbox with no network at all — how an organization's
        allowlist applies to commands, which a host list cannot hold. `secrets` are the
        organization's team secrets, as environment variables in the sandbox only."""
        if not command.strip():
            raise TerminalError("there is no command to run")
        timeout_s = max(1.0, min(MAX_TIMEOUT_S, timeout_s))
        if local:
            if not self.local_allowed:
                raise TerminalError(
                    "running commands on this machine is turned off (COMPUTER_LOCAL_COMMANDS)"
                )
            argv, env, cwd = ["/bin/bash", "-c", command], self._env(local=True), self._root
        else:
            if self.bwrap is None:
                raise TerminalError(
                    "the sandbox (bubblewrap, `bwrap`) is not installed on the computer"
                )
            argv, env, cwd = (
                self._sandboxed(command, network=network),
                {**(secrets or {}), **self._env(local=False)},
                self._root,
            )
        lock = self._locks.setdefault(screen_id, asyncio.Lock())
        async with lock:
            return await _execute(argv, env, cwd, timeout_s, mode="local" if local else "sandbox")

    def _env(self, *, local: bool) -> dict[str, str]:
        env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if local:
            env["HOME"] = os.environ.get("HOME", str(self._root))
            env["USER"] = os.environ.get("USER", "")
        else:
            env["HOME"] = "/workspace"
        env["WORKSPACE"] = "/workspace" if not local else str(self._root)
        return env

    def _sandboxed(self, command: str, *, network: bool = True) -> list[str]:
        argv = [
            self.bwrap or "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108 - the sandbox's own private tmpfs, not the host's /tmp
            "--bind",
            str(self._root),
            "/workspace",
            "--chdir",
            "/workspace",
        ]
        if self.network and network:
            argv.append("--share-net")
        for top in ("bin", "lib", "lib64", "sbin"):
            target = Path("/") / top
            if target.is_symlink():
                argv += ["--symlink", os.readlink(target), f"/{top}"]
            elif target.exists():
                argv += ["--ro-bind", str(target), f"/{top}"]
        for path in _RO_ETC:
            if os.path.exists(path):
                argv += ["--ro-bind", path, path]
        # The environment is not on this command line: bwrap is started with exactly the
        # sandbox's (`run`), and its child inherits it — so a team secret is never in
        # an argument another user on this machine could read in the process table.
        return [*argv, "/bin/bash", "-c", command]

    # --- the workspace -------------------------------------------------------------------

    def resolve(self, raw: str) -> Path:
        """`/workspace/a/b.csv`, `a/b.csv` or `/a/b.csv` → a path inside the workspace."""
        text = (raw or "").strip()
        for prefix in ("/workspace/", "/workspace"):
            if text == prefix.rstrip("/") or text.startswith(prefix):
                text = text[len(prefix) :]
                break
        candidate = (self._root / text.lstrip("/")).resolve()
        if candidate != self._root and not candidate.is_relative_to(self._root):
            raise TerminalError(f"{raw} is outside /workspace")
        return candidate

    def shown(self, path: Path) -> str:
        """A path as a bot names it. Not resolved: a link in the workspace is shown where
        it is, whatever it points at."""
        try:
            rel = path.relative_to(self._root).as_posix()
        except ValueError:
            rel = path.resolve().relative_to(self._root).as_posix()
        return "/workspace" + ("" if rel == "." else f"/{rel}")

    def listing(self, raw: str = "/workspace") -> list[dict[str, Any]]:
        folder = self.resolve(raw)
        if not folder.is_dir():
            raise TerminalError(f"{raw} is not a folder in /workspace")
        out: list[dict[str, Any]] = []

        def order(p: Path) -> tuple[bool, str]:
            return (p.is_symlink() or not p.is_dir(), p.name.lower())

        for entry in sorted(folder.iterdir(), key=order):
            try:
                stat = entry.lstat()
            except OSError:
                continue
            # A link is listed, never followed: where it points may be outside.
            link = entry.is_symlink()
            is_folder = not link and entry.is_dir()
            out.append(
                {
                    "name": entry.name,
                    "path": self.shown(entry),
                    "folder": is_folder,
                    "link": link,
                    "bytes": 0 if is_folder or link else stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
            if len(out) >= LIST_ENTRIES:
                break
        return out

    def read(self, raw: str) -> tuple[Path, bytes]:
        path = self.resolve(raw)
        if not path.is_file():
            raise TerminalError(f"there is no file {raw} in /workspace")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise TerminalError(
                f"{raw} is {size / 1_048_576:.1f} MB; files over "
                f"{MAX_FILE_BYTES // 1_048_576} MB stay in the workspace"
            )
        return path, path.read_bytes()

    def write(self, raw: str, data: bytes) -> Path:
        if len(data) > MAX_FILE_BYTES:
            raise TerminalError(f"files over {MAX_FILE_BYTES // 1_048_576} MB cannot be copied")
        path = self.resolve(raw)
        if path == self._root or raw.strip().endswith("/"):
            raise TerminalError(f"{raw} is a folder; name the file, e.g. {raw.rstrip('/')}/a.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def free_download_path(self, suggested: str) -> Path:
        """Where a browser download goes: `downloads/<name>`, numbered when taken."""
        name = "".join(c for c in suggested if c not in '/\\:*?"<>|\x00').strip(" .") or "download"
        folder = self._root / "downloads"
        folder.mkdir(exist_ok=True)
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        for n in range(1, 1000):
            candidate = folder / (name if n == 1 else f"{stem}-{n}" + (f".{ext}" if ext else ""))
            if not candidate.exists():
                return candidate
        raise TerminalError("too many downloads with that name")


async def _execute(
    argv: list[str], env: dict[str, str], cwd: Path, timeout_s: float, *, mode: str
) -> Ran:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    kept = {"stdout": bytearray(), "stderr": bytearray()}
    cut = {"stdout": False, "stderr": False}

    async def drain(stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        while chunk := await stream.read(8192):
            room = OUTPUT_BYTES - len(kept[name])
            if room > 0:
                kept[name] += chunk[:room]
            if len(chunk) > room:
                cut[name] = True

    readers = [
        asyncio.create_task(drain(process.stdout, "stdout")),
        asyncio.create_task(drain(process.stderr, "stderr")),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
    # The command's own process group, whatever it left running in the background — a
    # `server &` would otherwise hold the pipes open and outlive the command it was.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=5)
    for reader in readers:
        reader.cancel()
    stdout = bytes(kept["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(kept["stderr"]).decode("utf-8", errors="replace")
    if timed_out:
        stderr += f"\n(stopped: the command ran past its {timeout_s:.0f}s timeout)"
    return Ran(
        exit_code=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=cut["stdout"] or cut["stderr"],
        timed_out=timed_out,
        seconds=time.monotonic() - started,
        mode=mode,
    )
