"""Linux implementation of the daemon-installer interface, via a
`systemd --user` unit — the direct Linux analogue of macos_launchd.py's
LaunchAgent. See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md
— this is the ONLY file that should ever shell out to `systemctl --user` /
`loginctl` or know the unit file's shape. Callers use
claude_unlimited.daemon_installer, never this module directly.

UNVERIFIED against real hardware, but written to close the gaps a portability
review found: systemd may be absent (WSL2 default, containers), the daemon
must survive logout (lingering), a re-install must actually pick up a new port,
and a crash-loop must not permanently disable the unit.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

UNIT_NAME = "claude-unlimited.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PATH = UNIT_DIR / UNIT_NAME
LOG_DIR = Path.home() / ".claude-unlimited" / "logs"


class DaemonInstallerError(RuntimeError):
    pass


def _run_systemctl(*args: str):
    # FileNotFoundError (systemd not installed) is turned into our own error
    # rather than escaping as a raw OSError — every other subprocess site in
    # the codebase does this, and without it `install`/`status`/`doctor` all
    # traceback on a systemd-less box (WSL2 without systemd, minimal containers).
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise DaemonInstallerError(
            "`systemctl` was not found — this Linux environment does not appear to run "
            "systemd (e.g. WSL2 without `systemd=true`, or a minimal container). Run the "
            "daemon in the foreground with `claude-unlimited start` instead, or enable "
            "systemd for your user session."
        ) from None


def _systemd_version() -> int:
    """Major version of the running systemd, or 0 if it can't be determined.

    `StandardOutput=append:` only exists in systemd >= 240; on older releases
    it is ignored and logs silently go to the journal instead — so the log
    files the CLI points people at would never appear. We check and fall back."""
    try:
        out = subprocess.run(["systemctl", "--version"], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return 0
    # First line: "systemd 249 (249.11-0ubuntu3)"
    for token in (out.stdout or "").split():
        if token.isdigit():
            return int(token)
    return 0


def _unit_contents(port: int) -> str:
    import sys

    python = shlex.quote(sys.executable)   # $HOME may contain spaces → quote
    exec_start = f"{python} -m claude_unlimited start --port {port}"

    # Logs: append: needs systemd >= 240; otherwise omit and let it journal.
    log_lines = ""
    if _systemd_version() >= 240:
        out_log = shlex.quote(str(LOG_DIR / "daemon.out.log"))
        err_log = shlex.quote(str(LOG_DIR / "daemon.err.log"))
        log_lines = f"StandardOutput=append:{out_log}\nStandardError=append:{err_log}\n"

    return (
        "[Unit]\n"
        "Description=Claude Unlimited daemon\n"
        "After=default.target\n"
        # A daemon that fails fast (e.g. the port is briefly held during an
        # upgrade) must not burn the default 5-starts-in-10s budget and then be
        # left `failed` forever needing a manual reset-failed. 0 disables the
        # rate limiter, matching the macOS LaunchAgent's retry-indefinitely
        # KeepAlive behaviour.
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        f"{log_lines}"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _enable_linger() -> None:
    """Keep the daemon alive after logout. Best-effort and non-fatal.

    A `systemd --user` service is killed when the user's last session ends
    unless lingering is enabled — which would silently break "always there for
    Claude Code" on Linux. With standard polkit a user may linger their OWN
    account without root; on a hardened system it is denied, in which case we
    tell them the one command to run and carry on rather than failing install."""
    user = None
    try:
        import getpass

        user = getpass.getuser()
        result = subprocess.run(["loginctl", "enable-linger", user],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    print(
        "  Note: could not enable lingering automatically, so the daemon will stop when "
        "you log out. To keep it running across logouts, run:\n"
        f"      loginctl enable-linger {user or '$USER'}",
        flush=True,
    )


def install(port: int) -> None:
    """Writes the unit, enables it, starts it, and enables lingering. Safe to
    call again to change the port — it restarts, so the new port takes effect."""
    # Fail before writing the unit file if systemd is unusable, so a failed
    # install never leaves an orphan unit that makes every later status/doctor
    # call crash.
    probe = _run_systemctl("is-system-running")  # raises DaemonInstallerError if no systemctl
    del probe  # return code is irrelevant; we only needed it not to raise

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(_unit_contents(port), encoding="utf-8")

    try:
        reload_result = _run_systemctl("daemon-reload")
        if reload_result.returncode != 0:
            raise DaemonInstallerError(
                f"systemctl --user daemon-reload failed: {(reload_result.stderr or reload_result.stdout).strip()}")

        enable_result = _run_systemctl("enable", UNIT_NAME)
        if enable_result.returncode != 0:
            raise DaemonInstallerError(
                f"systemctl --user enable failed: {(enable_result.stderr or enable_result.stdout).strip()}")

        # restart, not `enable --now`: start is a no-op on an already-running
        # unit, so a re-install to change the port would leave the old process
        # serving the old port. restart picks up the new unit every time.
        restart_result = _run_systemctl("restart", UNIT_NAME)
        if restart_result.returncode != 0:
            raise DaemonInstallerError(
                f"systemctl --user restart failed: {(restart_result.stderr or restart_result.stdout).strip()}")
    except DaemonInstallerError:
        # Leave the machine as we found it: a half-registered unit that then
        # makes status()/doctor() misreport is worse than no unit.
        UNIT_PATH.unlink(missing_ok=True)
        _run_systemctl("daemon-reload")
        raise

    _enable_linger()


def uninstall() -> None:
    _run_systemctl("disable", "--now", UNIT_NAME)  # ignore failure: fine if it wasn't enabled/running
    UNIT_PATH.unlink(missing_ok=True)
    _run_systemctl("daemon-reload")
    # Undo the machine-policy change install made — leave nothing behind.
    try:
        import getpass

        subprocess.run(["loginctl", "disable-linger", getpass.getuser()],
                       capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        pass


def is_installed() -> bool:
    return UNIT_PATH.exists()


def status() -> dict:
    if not is_installed():
        return {"installed": False, "running": False, "pid": None}

    try:
        result = _run_systemctl("show", "--property=MainPID,ActiveState", UNIT_NAME)
    except DaemonInstallerError:
        # systemd disappeared but the unit file is still on disk — report
        # not-running rather than crashing the caller.
        return {"installed": True, "running": False, "pid": None}
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
