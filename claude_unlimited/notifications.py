"""Desktop notifications via one OS-native mechanism per platform, none of
them an extra dependency: `osascript` on macOS, `notify-send` on Linux, a
PowerShell toast on Windows. The Linux and Windows paths are UNVERIFIED —
treat them as a first cut, not confirmed backends.

Every call site funnels through notify_if_enabled(), the single place that
checks both the master notifications_enabled switch and the per-category
Settings.notify_* flag.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .config import Settings

CATEGORY_TO_SETTINGS_FIELD = {
    "update_available": "notify_update_available",
    "approaching_threshold": "notify_approaching_threshold",
    "rotated": "notify_rotated",
    "quota_reset": "notify_quota_reset",
    "needs_attention": "notify_needs_attention",
}


def _applescript_string_literal(text: str) -> str:
    # AppleScript string literal escaping: backslash and double-quote only.
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_macos_notification(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{_applescript_string_literal(message)}" with title "{_applescript_string_literal(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort: a notification must never break a request


def send_linux_notification(title: str, message: str) -> None:
    if sys.platform != "linux" or shutil.which("notify-send") is None:
        return  # no libnotify provider: degrade to no notification, never an error
    try:
        subprocess.run(["notify-send", "--app-name=Claude Unlimited", title, message],
                        capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _powershell_string_literal(text: str) -> str:
    # PowerShell single-quoted string literal escaping: only ' needs doubling.
    return text.replace("'", "''")


def send_windows_notification(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    # BurntToast isn't built in, so this drives the WinRT toast API directly
    # rather than take a dependency.
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null;"
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$texts = $template.GetElementsByTagName('text');"
        f"$texts.Item(0).AppendChild($template.CreateTextNode('{_powershell_string_literal(title)}')) | Out-Null;"
        f"$texts.Item(1).AppendChild($template.CreateTextNode('{_powershell_string_literal(message)}')) | Out-Null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Unlimited')."
        "Show($toast);"
    )
    try:
        # CREATE_NO_WINDOW: the daemon runs without a console, so PowerShell
        # would otherwise flash a console window for every notification sent.
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                        capture_output=True, timeout=5, check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def send(title: str, message: str) -> None:
    """Fire a desktop notification on whatever platform this is. Each backend
    self-guards on sys.platform, so calling all three sends exactly one. This
    is the single dispatch point — the "test notification" button must use it,
    not one platform's sender, or it silently does nothing off that platform."""
    send_macos_notification(title, message)
    send_linux_notification(title, message)
    send_windows_notification(title, message)


def notify_if_enabled(category: str, title: str, message: str, settings: Settings) -> None:
    field = CATEGORY_TO_SETTINGS_FIELD.get(category)
    if field is None:
        raise ValueError(f"Unknown notification category {category!r}.")
    if not settings.notifications_enabled or not getattr(settings, field):
        return
    send(title, message)
