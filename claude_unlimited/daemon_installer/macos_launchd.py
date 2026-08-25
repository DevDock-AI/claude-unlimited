"""macOS implementation of the daemon-installer interface, via launchd.

See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is
the ONLY file that should ever shell out to `launchctl` or know a launchd
plist's shape. Callers use claude_unlimited.daemon_installer, never this
module directly.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.claude-unlimited.daemon"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
LOG_DIR = Path.home() / ".claude-unlimited" / "logs"


class DaemonInstallerError(RuntimeError):
    pass


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(port: int) -> None:
    """Writes the LaunchAgent plist and registers it to auto-start on login.
    Safe to call again to pick up a changed port — bootout-then-bootstrap
    is idempotent, unlike bootstrap alone (which errors if already loaded)."""

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "claude_unlimited", "start", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_DIR / "daemon.out.log"),
        "StandardErrorPath": str(LOG_DIR / "daemon.err.log"),
    }
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    _run_launchctl("bootout", f"{_domain_target()}/{LABEL}")  # ignore failure: fine if it wasn't loaded
    result = _run_launchctl("bootstrap", _domain_target(), str(PLIST_PATH))
    if result.returncode != 0:
        raise DaemonInstallerError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")


def uninstall() -> None:
    _run_launchctl("bootout", f"{_domain_target()}/{LABEL}")  # ignore failure: fine if it wasn't loaded
    PLIST_PATH.unlink(missing_ok=True)


def is_installed() -> bool:
    return PLIST_PATH.exists()


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}

    result = _run_launchctl("print", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        return {"installed": True, "running": False, "pid": None}

    pid = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
            break
    return {"installed": True, "running": pid is not None, "pid": pid}


def start() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — run `claude-unlimited install` first.")
    result = _run_launchctl("kickstart", "-k", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        raise DaemonInstallerError(f"launchctl kickstart failed: {(result.stderr or result.stdout).strip()}")


def stop() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — nothing to stop.")
    result = _run_launchctl("kill", "SIGTERM", f"{_domain_target()}/{LABEL}")
    if result.returncode != 0:
        raise DaemonInstallerError(f"launchctl kill failed: {(result.stderr or result.stdout).strip()}")
