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


def test_login_import_error_names_this_platforms_location(monkeypatch):
    """The "no login" error used to name "macOS Keychain" on every OS, sending
    Windows and Linux users hunting for something that does not exist there.
    Each platform must describe where ITS login actually lives, and say that
    the CLI has to have been signed in first."""
    from claude_unlimited import anthropic_oauth

    seen = {}
    for system, expected in (("Darwin", "Keychain"), ("Windows", ".credentials.json"),
                             ("Linux", ".credentials.json")):
        monkeypatch.setattr(anthropic_oauth.platform, "system", lambda s=system: s)
        message = anthropic_oauth.no_default_login_message()
        seen[system] = message
        assert expected in message
        # the prerequisite is the actual fix: nothing works until the CLI logs in
        assert "/login" in message and "at least once" in message

    assert "Keychain" not in seen["Windows"], seen["Windows"]
    assert "Keychain" not in seen["Linux"], seen["Linux"]
    assert "Windows PC" in seen["Windows"] and "Mac" in seen["Darwin"]
    assert "Linux machine" in seen["Linux"]


def test_login_import_error_names_a_cli_that_actually_exists(monkeypatch, tmp_path):
    """"Run `claude`" is a dead end for desktop-app users: the app ships its own
    copy of the CLI inside its userData directory and puts nothing on PATH. The
    message must name the bundled binary, newest version first."""
    from claude_unlimited import anthropic_oauth

    monkeypatch.setattr(anthropic_oauth.platform, "system", lambda: "Windows")
    monkeypatch.setattr(anthropic_oauth.shutil, "which", lambda _name: None)
    # home too, or this machine's own ~/.local/bin install answers instead
    monkeypatch.setattr(anthropic_oauth.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for version in ("2.1.247", "2.1.9", "1.0.0"):
        exe = tmp_path / "Claude" / "claude-code" / version / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")

    # 2.1.247 > 2.1.9 only if the version is compared numerically, not as text.
    assert "2.1.247" in anthropic_oauth.claude_cli_command()
    assert "2.1.247" in anthropic_oauth.no_default_login_message()

    # ...and when the CLI is genuinely absent, say so instead of naming nothing.
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
    assert anthropic_oauth.claude_cli_command() == ""
    assert "not installed" in anthropic_oauth.no_default_login_message()


def test_login_import_error_prefers_a_cli_on_path(monkeypatch):
    """When `claude` IS on PATH, keep the short instruction."""
    from claude_unlimited import anthropic_oauth

    monkeypatch.setattr(anthropic_oauth.shutil, "which", lambda _name: "/usr/local/bin/claude")
    assert anthropic_oauth.claude_cli_command() == "claude"
    assert "Run `claude` in a terminal" in anthropic_oauth.no_default_login_message()


def test_login_import_error_finds_a_natively_installed_cli(monkeypatch, tmp_path):
    """Claude Code's own installer writes to ~/.local/bin and updates PATH, but a
    terminal opened before that still cannot resolve `claude` - so a real install
    must be found there before falling back to the desktop app's older bundle."""
    from claude_unlimited import anthropic_oauth

    monkeypatch.setattr(anthropic_oauth.platform, "system", lambda: "Windows")
    monkeypatch.setattr(anthropic_oauth.shutil, "which", lambda _name: None)
    monkeypatch.setattr(anthropic_oauth.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    bundled = tmp_path / "Roaming" / "Claude" / "claude-code" / "2.1.247" / "claude.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    assert "2.1.247" in anthropic_oauth.claude_cli_command()   # only the bundle exists

    native = tmp_path / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    command = anthropic_oauth.claude_cli_command()
    assert ".local" in command and "2.1.247" not in command    # the real install wins
