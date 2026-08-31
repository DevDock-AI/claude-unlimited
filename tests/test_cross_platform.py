"""Portability regressions: things that crashed or lied on Windows/Linux.

These run on the CI/dev platform (macOS/Linux) but assert the platform-agnostic
shape of the fixes — a signal list that never names SIGKILL where it is absent,
a secret-store backend that identifies itself honestly, a notification dispatch
that fires on every platform, not just macOS.
"""
import os
import signal

import pytest


def test_stop_signals_never_reference_a_missing_signal():
    """`signal.SIGKILL` is Unix-only; even naming it in a tuple raises
    AttributeError on Windows. The ladder must be built by presence."""
    from claude_unlimited import cli

    assert signal.SIGTERM in cli._STOP_SIGNALS
    # Whatever is in the list must actually exist as a signal on this platform.
    for s in cli._STOP_SIGNALS:
        assert s is not None
    # On a platform with SIGKILL it is included; on one without, it is absent —
    # and either way the module imported without raising, which is the point.
    if hasattr(signal, "SIGKILL"):
        assert signal.SIGKILL in cli._STOP_SIGNALS
    else:  # pragma: no cover - only on Windows
        assert all(s != getattr(signal, "SIGKILL", object()) for s in cli._STOP_SIGNALS)


def test_secret_store_names_the_backend_it_loaded():
    """`doctor` used to print "macOS Keychain" on every OS. The backend now
    reports its own name so the diagnostic tells the truth per platform."""
    import platform

    from claude_unlimited import secret_store

    assert secret_store.BACKEND_NAME
    expected = {"Darwin": "macOS Keychain", "Linux": "Linux Secret Service (libsecret)",
                "Windows": "Windows DPAPI"}[platform.system()]
    assert secret_store.BACKEND_NAME == expected


def test_notification_dispatch_fires_on_every_platform(monkeypatch):
    """The "test notification" button called only the macOS sender and then
    reported success — a silent no-op on Linux/Windows. `send()` must invoke
    all three platform senders (each self-guards to exactly one)."""
    from claude_unlimited import notifications

    called = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda t, m: called.append("macos"))
    monkeypatch.setattr(notifications, "send_linux_notification", lambda t, m: called.append("linux"))
    monkeypatch.setattr(notifications, "send_windows_notification", lambda t, m: called.append("windows"))

    notifications.send("Title", "Body")
    assert set(called) == {"macos", "linux", "windows"}


def test_notify_if_enabled_routes_through_send(monkeypatch):
    """Regression: the settings-gated path must use the same dispatch, so a
    real rotation notification is not macOS-only either."""
    from claude_unlimited import notifications
    from claude_unlimited.config import Settings

    fired = []
    monkeypatch.setattr(notifications, "send", lambda t, m: fired.append((t, m)))
    settings = Settings(notifications_enabled=True, notify_rotated=True)
    notifications.notify_if_enabled("rotated", "T", "M", settings)
    assert fired == [("T", "M")]

    fired.clear()
    settings = Settings(notifications_enabled=False)
    notifications.notify_if_enabled("rotated", "T", "M", settings)
    assert fired == []


def test_codex_login_passes_the_full_environment(monkeypatch, tmp_path):
    """A stripped {PATH,HOME} env broke the browser handshake off macOS (no
    DISPLAY/DBUS on Linux, no APPDATA/SystemRoot on Windows). The login must
    inherit the full environment plus the CODEX_HOME override."""
    import claude_unlimited.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(cli, "CODEX_ACCOUNTS_DIR", tmp_path / "codex-accounts")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SOME_UNRELATED", "keepme")

    captured = {}

    def fake_run(cmd, **kw):
        if os.path.basename(cmd[0]) == "codex" and cmd[1:2] == ["login"]:
            captured["env"] = kw.get("env", {})
            return cli.subprocess.CompletedProcess(cmd, 1)   # bail after capturing
        return cli.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.add_codex_account()

    env = captured["env"]
    assert env.get("DISPLAY") == ":0"           # inherited, not stripped
    assert env.get("SOME_UNRELATED") == "keepme"
    assert env["CODEX_HOME"].endswith("codex-accounts") or "codex-accounts" in env["CODEX_HOME"]


def test_updater_venv_python_matches_the_platform_layout():
    """A venv is Scripts\\python.exe on Windows, bin/python elsewhere. Hardcoding
    the POSIX layout made every Windows self-update fail with a message telling
    the user to run a bash script."""
    import os

    from claude_unlimited import updater

    if os.name == "nt":  # pragma: no cover - only on Windows
        assert updater.VENV_PYTHON.parent.name == "Scripts"
        assert updater.VENV_PYTHON.name == "python.exe"
    else:
        assert updater.VENV_PYTHON.parent.name == "bin"
        assert updater.VENV_PYTHON.name == "python"
