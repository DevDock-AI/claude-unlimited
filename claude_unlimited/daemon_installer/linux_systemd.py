"""Linux implementation of the daemon-installer interface, via a
`systemd --user` unit — the direct Linux analogue of macos_launchd.py's
LaunchAgent. See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md
— this is the ONLY file that should ever shell out to `systemctl --user` or
know the unit file's shape. Callers use claude_unlimited.daemon_installer,
never this module directly.

UNVERIFIED: written to the same interface contract as macos_launchd.py and to
systemd's documented unit-file/systemctl behavior, but not yet exercised
against a live systemd user session. See CONTRIBUTING.md's "third-OS smoke
test" guidance for what closes that gap.

Known limitation: a `systemd --user` service only runs while there is an
active login session, unless lingering is enabled separately (`loginctl
enable-linger $USER`, which needs elevated privileges and is a machine-wide
policy decision, so this daemon does not enable it). A macOS LaunchAgent has
the same "runs while logged in" behavior.
"""

from __future__ import annotations

from pathlib import Path

UNIT_NAME = "claude-unlimited.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PATH = UNIT_DIR / UNIT_NAME
LOG_DIR = Path.home() / ".claude-unlimited" / "logs"


class DaemonInstallerError(RuntimeError):
    pass


def _run_systemctl(*args: str):
    import subprocess

    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _unit_contents(port: int) -> str:
    import sys

    python = sys.executable
    return (
        "[Unit]\n"
        "Description=Claude Unlimited daemon\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        f'ExecStart={python} -m claude_unlimited start --port {port}\n'
        "Restart=on-failure\n"
        "RestartSec=2\n"
        f"StandardOutput=append:{LOG_DIR / 'daemon.out.log'}\n"
        f"StandardError=append:{LOG_DIR / 'daemon.err.log'}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(port: int) -> None:
    """Writes the unit file and enables + starts it now. Safe to call again
    to pick up a changed port — daemon-reload + enable --now is idempotent."""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(_unit_contents(port), encoding="utf-8")

    reload_result = _run_systemctl("daemon-reload")
    if reload_result.returncode != 0:
        raise DaemonInstallerError(f"systemctl --user daemon-reload failed: {(reload_result.stderr or reload_result.stdout).strip()}")

    result = _run_systemctl("enable", "--now", UNIT_NAME)
    if result.returncode != 0:
        raise DaemonInstallerError(f"systemctl --user enable --now failed: {(result.stderr or result.stdout).strip()}")


def uninstall() -> None:
    _run_systemctl("disable", "--now", UNIT_NAME)  # ignore failure: fine if it wasn't enabled/running
    UNIT_PATH.unlink(missing_ok=True)
    _run_systemctl("daemon-reload")


def is_installed() -> bool:
    return UNIT_PATH.exists()


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}

    result = _run_systemctl("show", "--property=MainPID,ActiveState", UNIT_NAME)
    if result.returncode != 0:
        return {"installed": True, "running": False, "pid": None}

    props = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v.strip()

    pid = None
    try:
        pid_value = int(props.get("MainPID", "0"))
        pid = pid_value if pid_value > 0 else None
    except ValueError:
        pid = None
    running = props.get("ActiveState") == "active" and pid is not None
    return {"installed": True, "running": running, "pid": pid}


def start() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — run `claude-unlimited install` first.")
    result = _run_systemctl("restart", UNIT_NAME)  # restart, not start: idempotent either way, matches launchd's kickstart -k
    if result.returncode != 0:
        raise DaemonInstallerError(f"systemctl --user restart failed: {(result.stderr or result.stdout).strip()}")


def stop() -> None:
    if not is_installed():
        raise DaemonInstallerError("Not installed — nothing to stop.")
    result = _run_systemctl("stop", UNIT_NAME)
    if result.returncode != 0:
        raise DaemonInstallerError(f"systemctl --user stop failed: {(result.stderr or result.stdout).strip()}")
