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


# schtasks/tasklist/taskkill are console programs, and a process that has no
# console of its own — which the daemon is, being started hidden — gets a NEW
# console window allocated for each one it runs. The Dashboard polls status
# once a second, so without this flag the desktop flashes a console window
# every second for as long as the Dashboard is open.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True,
                          creationflags=_NO_WINDOW)


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
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _tasklist_line(pid: int) -> str:
    return _run("tasklist", "/fi", f"PID eq {pid}", "/nh").stdout


def _pid_is_alive(pid: int) -> bool:
    return str(pid) in _tasklist_line(pid)


def _pid_is_ours(pid: int) -> bool:
    """A live pid whose image is a Python interpreter. Windows recycles pids
    aggressively, and our pidfile is stale after every unclean stop, so before
    force-killing we confirm the pid is at least a python process — never
    `taskkill /f` an unrelated program that happened to inherit the number.
    (tasklist has no command line without /v; the image check is the cheap,
    safe floor.)"""
    line = _tasklist_line(pid).lower()
    return str(pid) in line and ("python.exe" in line or "pythonw.exe" in line)


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}
    pid = _read_pid()
    running = pid is not None and _pid_is_alive(pid)
    return {"installed": True, "running": running, "pid": pid if running else None}


def start() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — run `claude-unlimited install` first.")
    # The caller may BE the daemon (Dashboard "Restart", auto-update). If this
    # process ran taskkill-on-itself then `schtasks /run`, the /run line would
    # never execute — the daemon would kill itself and stay dead. Unlike
    # launchd/systemd (external supervisors), schtasks is just a CLI we invoke,
    # so the restart has to be carried by a helper that OUTLIVES this process.
    _detached_restart()


def _detached_restart() -> None:
    """Spawn a detached process that kills the old instance, waits for it to
    exit, then starts the task — surviving the death of whoever called start()."""
    pid = _read_pid()
    script = (
        "import subprocess, time\n"
        # Same reason as _run's flag: this helper is detached and console-less,
        # so each console program it runs would otherwise pop a window.
        "NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
        f"pid = {pid!r}\n"
        "if pid:\n"
        "    subprocess.run(['taskkill', '/pid', str(pid), '/f'], capture_output=True,\n"
        "                   creationflags=NW)\n"
        "    for _ in range(60):\n"
        "        r = subprocess.run(['tasklist', '/fi', 'PID eq ' + str(pid), '/nh'],\n"
        "                           capture_output=True, text=True, creationflags=NW)\n"
        "        if str(pid) not in r.stdout:\n"
        "            break\n"
        "        time.sleep(0.25)\n"
        f"subprocess.run(['schtasks', '/run', '/tn', {TASK_NAME!r}], capture_output=True,\n"
        "               creationflags=NW)\n"
    )
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [sys.executable, "-c", script],
        creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — nothing to stop.")
    pid = _read_pid()
    if pid is None or not _pid_is_alive(pid):
        raise DaemonInstallerError("Task is registered but no running instance was found (pidfile stale or missing).")
    if not _pid_is_ours(pid):
        raise DaemonInstallerError(
            f"Pid {pid} from the pidfile is not one of our daemon processes (a recycled pid). "
            "Refusing to kill it.")
    result = _run("taskkill", "/pid", str(pid), "/f")
    if result.returncode != 0:
        raise DaemonInstallerError(f"taskkill failed: {(result.stderr or result.stdout).strip()}")
