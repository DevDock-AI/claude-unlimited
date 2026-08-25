"""Windows implementation of the daemon-installer interface, via a Task
Scheduler task (`schtasks`, built into every Windows install — no extra
dependency) triggered on logon — the Windows analogue of macos_launchd.py's
LaunchAgent. See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md
— this is the ONLY file that should ever shell out to `schtasks`/`tasklist`/
`taskkill` or know the task's shape. Callers use
claude_unlimited.daemon_installer, never this module directly.

UNVERIFIED: written to `schtasks`' documented CLI contract but not yet
exercised on Windows. See CONTRIBUTING.md's "third-OS smoke test" guidance
for what closes that gap.

Unlike launchd/systemd, Task Scheduler doesn't expose a running task's
child-process pid, so `status()`/`start()`/`stop()` read the pidfile the
daemon writes on startup (daemon.py's PID_FILE, ~/.claude-unlimited/
daemon.pid) and confirm that pid is alive via `tasklist` before trusting it —
a stale pidfile from an unclean shutdown must never be reported as running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "ClaudeUnlimitedDaemon"
PID_FILE = Path.home() / ".claude-unlimited" / "daemon.pid"


class DaemonInstallerError(RuntimeError):
    pass


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def install(port: int) -> None:
    """Registers the logon-triggered task. Safe to call again to pick up a
    changed port — /create with /f overwrites an existing task of the same
    name rather than erroring."""
    # pythonw.exe, if present alongside the running interpreter, avoids
    # popping a console window on logon; fall back to the interpreter itself
    # (e.g. a venv that only ships python.exe).
    python = sys.executable
    pythonw = Path(python).with_name("pythonw.exe")
    launcher = str(pythonw) if pythonw.exists() else python

    result = _run(
        "schtasks", "/create", "/tn", TASK_NAME,
        "/tr", f'"{launcher}" -m claude_unlimited start --port {port}',
        "/sc", "onlogon", "/rl", "limited", "/f",
    )
    if result.returncode != 0:
        raise DaemonInstallerError(f"schtasks /create failed: {(result.stderr or result.stdout).strip()}")
    start()


def uninstall() -> None:
    try:
        stop()
    except DaemonInstallerError:
        pass
    _run("schtasks", "/delete", "/tn", TASK_NAME, "/f")  # ignore failure: fine if it wasn't registered


def is_installed() -> bool:
    result = _run("schtasks", "/query", "/tn", TASK_NAME)
    return result.returncode == 0


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    result = _run("tasklist", "/fi", f"PID eq {pid}", "/nh")
    return str(pid) in result.stdout


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}
    pid = _read_pid()
    running = pid is not None and _pid_is_alive(pid)
    return {"installed": True, "running": running, "pid": pid if running else None}


def start() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — run `claude-unlimited install` first.")
    _stop_running_instance()  # atomic-ish stop-then-start, matching launchd's kickstart -k
    result = _run("schtasks", "/run", "/tn", TASK_NAME)
    if result.returncode != 0:
        raise DaemonInstallerError(f"schtasks /run failed: {(result.stderr or result.stdout).strip()}")


def _stop_running_instance() -> None:
    pid = _read_pid()
    if pid is not None and _pid_is_alive(pid):
        _run("taskkill", "/pid", str(pid), "/f")


def stop() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — nothing to stop.")
    pid = _read_pid()
    if pid is None or not _pid_is_alive(pid):
        raise DaemonInstallerError("Task is registered but no running instance was found (pidfile stale or missing).")
    result = _run("taskkill", "/pid", str(pid), "/f")
    if result.returncode != 0:
        raise DaemonInstallerError(f"taskkill failed: {(result.stderr or result.stdout).strip()}")
