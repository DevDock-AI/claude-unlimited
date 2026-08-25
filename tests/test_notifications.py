import pytest

import claude_unlimited.notifications as notifications
from claude_unlimited.config import Settings

# Bound directly rather than via the module attribute, so these tests exercise
# the real implementations despite conftest.py's autouse no-op fixture:
# rebinding a module attribute doesn't affect a name already bound by
# `from ... import ...`.
from claude_unlimited.notifications import send_macos_notification as real_send_macos_notification
from claude_unlimited.notifications import send_linux_notification as real_send_linux_notification
from claude_unlimited.notifications import send_windows_notification as real_send_windows_notification


def test_applescript_string_literal_escapes_quotes_and_backslashes():
    assert notifications._applescript_string_literal('say "hi" \\ bye') == 'say \\"hi\\" \\\\ bye'


def test_send_macos_notification_calls_osascript_on_darwin(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_macos_notification("Title", "Body")
    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[0] == "osascript"
    assert "Title" in args[2]
    assert "Body" in args[2]


def test_send_macos_notification_noop_on_non_darwin(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_macos_notification("Title", "Body")
    assert calls == []


def test_send_macos_notification_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setattr(notifications.sys, "platform", "darwin")

    def boom(*a, **kw):
        raise OSError("no osascript")

    monkeypatch.setattr(notifications.subprocess, "run", boom)
    real_send_macos_notification("Title", "Body")  # must not raise


def test_powershell_string_literal_escapes_single_quotes():
    assert notifications._powershell_string_literal("it's a test") == "it''s a test"


def test_send_linux_notification_calls_notify_send_on_linux(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_linux_notification("Title", "Body")
    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[0] == "notify-send"
    assert "Title" in args and "Body" in args


def test_send_linux_notification_noop_on_non_linux(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_linux_notification("Title", "Body")
    assert calls == []


def test_send_linux_notification_noop_when_notify_send_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda name: None)
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_linux_notification("Title", "Body")
    assert calls == []


def test_send_linux_notification_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda name: "/usr/bin/notify-send")

    def boom(*a, **kw):
        raise OSError("no notify-send")

    monkeypatch.setattr(notifications.subprocess, "run", boom)
    real_send_linux_notification("Title", "Body")  # must not raise


def test_send_windows_notification_calls_powershell_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "win32")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_windows_notification("Title", "Body")
    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[0] == "powershell"
    script = args[-1]
    assert "Title" in script and "Body" in script


def test_send_windows_notification_noop_on_non_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    real_send_windows_notification("Title", "Body")
    assert calls == []


def test_send_windows_notification_swallows_subprocess_errors(monkeypatch):
    monkeypatch.setattr(notifications.sys, "platform", "win32")

    def boom(*a, **kw):
        raise OSError("no powershell")

    monkeypatch.setattr(notifications.subprocess, "run", boom)
    real_send_windows_notification("Title", "Body")  # must not raise


def test_notify_if_enabled_respects_master_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda t, m: calls.append((t, m)))
    settings = Settings(notifications_enabled=False, notify_rotated=True)
    notifications.notify_if_enabled("rotated", "T", "M", settings)
    assert calls == []


def test_notify_if_enabled_respects_category_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda t, m: calls.append((t, m)))
    settings = Settings(notifications_enabled=True, notify_rotated=False)
    notifications.notify_if_enabled("rotated", "T", "M", settings)
    assert calls == []


def test_notify_if_enabled_fires_when_both_on(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda t, m: calls.append(("macos", t, m)))
    monkeypatch.setattr(notifications, "send_linux_notification", lambda t, m: calls.append(("linux", t, m)))
    monkeypatch.setattr(notifications, "send_windows_notification", lambda t, m: calls.append(("windows", t, m)))
    settings = Settings(notifications_enabled=True, notify_rotated=True)
    notifications.notify_if_enabled("rotated", "T", "M", settings)
    # All three OS-specific senders are attempted; each no-ops internally on
    # the wrong platform, so calling all three unconditionally is correct.
    assert calls == [("macos", "T", "M"), ("linux", "T", "M"), ("windows", "T", "M")]


def test_notify_if_enabled_rejects_unknown_category():
    with pytest.raises(ValueError):
        notifications.notify_if_enabled("not_a_real_category", "T", "M", Settings())
