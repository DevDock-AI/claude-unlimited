"""Daemon install/auto-start interface: install/uninstall/is_installed/status/
start/stop. The single point that knows which OS backend to use — every
other module imports THIS package, never a specific backend module. See
docs/adr/0002-pluggable-secret-store-and-daemon-installer.md. Supporting a
new OS means adding a backend module and one branch here, with no call site
changes (the same shape as claude_unlimited.secret_store).
"""

from __future__ import annotations

import platform

_SYSTEM = platform.system()

if _SYSTEM == "Darwin":
    from .macos_launchd import DaemonInstallerError, install, is_installed, start, status, stop, uninstall
elif _SYSTEM == "Linux":
    # UNVERIFIED — see linux_systemd.py's module docstring.
    from .linux_systemd import DaemonInstallerError, install, is_installed, start, status, stop, uninstall
elif _SYSTEM == "Windows":
    # UNVERIFIED — see windows_taskscheduler.py's module docstring.
    from .windows_taskscheduler import DaemonInstallerError, install, is_installed, start, status, stop, uninstall
else:
    class DaemonInstallerError(RuntimeError):
        pass

    def _unsupported(*_args, **_kwargs):
        raise DaemonInstallerError(
            f"No daemon-installer backend for {_SYSTEM!r}. Claude Unlimited supports "
            "macOS, Linux, and Windows — see docs/adr/0002-pluggable-secret-store-and-daemon-installer.md "
            "for how to add one. Run `claude-unlimited start` to run the daemon in the "
            "foreground for now."
        )

    install = uninstall = start = stop = _unsupported

    def is_installed() -> bool:
        return False

    def status() -> dict:
        return {"installed": False, "running": False, "pid": None}

__all__ = ["install", "uninstall", "is_installed", "status", "start", "stop", "DaemonInstallerError"]
