import pytest

import claude_unlimited.config as config
import claude_unlimited.notifications as notifications
import claude_unlimited.usage_probe as usage_probe


@pytest.fixture(autouse=True)
def no_real_desktop_notifications(monkeypatch):
    """The test suite must never pop a real desktop notification.

    All three OS-specific senders are stubbed, because notify_if_enabled
    calls all three and each platform's own guard only helps off-platform.
    Tests that care about what would have fired can monkeypatch any of
    these again with their own recorder.
    """
    monkeypatch.setattr(notifications, "send_macos_notification", lambda title, message: None)
    monkeypatch.setattr(notifications, "send_linux_notification", lambda title, message: None)
    monkeypatch.setattr(notifications, "send_windows_notification", lambda title, message: None)


@pytest.fixture(autouse=True)
def no_real_user_config(monkeypatch, tmp_path):
    """The test suite must never read the owner's real ~/.claude-unlimited.

    Without this, any code path that calls load_pool() sees the live config —
    e.g. with Settings → "Balance sessions and subagents across accounts" on,
    `cli.code()` takes the balancing path and makes a real HTTP call to the
    running daemon. Tests that need a specific config still point these at
    their own tmp dir, which overrides this.
    """
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "app" / "config.json")


@pytest.fixture(autouse=True)
def no_real_usage_endpoint_calls(monkeypatch):
    """usage_probe's single network seam refuses by default; a test that
    exercises the HTTP layer installs its own fake."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach a real usage endpoint")
    monkeypatch.setattr(usage_probe, "_urlopen", refuse)

