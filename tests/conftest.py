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
    """The test suite must never read the user's real ~/.claude-unlimited.

    Without this, any code path that calls load_pool() sees the live config —
    e.g. with Settings → "Balance sessions and subagents across accounts" on,
    `cli.code()` takes the balancing path and makes a real HTTP call to the
    running daemon. Tests that need a specific config still point these at
    their own tmp dir, which overrides this.
    """
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "app" / "config.json")

    # These modules bind their file path from APP_DIR at IMPORT time, so the
    # patch above does not reach them. Missing this once let a test that went
    # through Gateway.force_active() (→ _persist) overwrite the user's live
    # runtime_state.json with test profile ids. Every import-time path that
    # something WRITES goes here.
    import claude_unlimited.activity as activity
    import claude_unlimited.daemon as daemon
    import claude_unlimited.placeholder_token as placeholder_token
    import claude_unlimited.project_usage as project_usage
    import claude_unlimited.runtime_state as runtime_state
    import claude_unlimited.session_tokens as session_tokens
    import claude_unlimited.usage_history as usage_history
    app = tmp_path / "app"
    monkeypatch.setattr(runtime_state, "RUNTIME_STATE_FILE", app / "runtime_state.json")
    monkeypatch.setattr(activity, "ACTIVITY_FILE", app / "activity.jsonl")
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", app / "placeholder_token")
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", app / "session_tokens.json")
    monkeypatch.setattr(project_usage, "USAGE_FILE", app / "project_usage.json")
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", app / "usage_history.jsonl")
    monkeypatch.setattr(daemon, "PID_FILE", app / "daemon.pid")

    # The HUD installer writes into ~/Applications and ~/Library/LaunchAgents.
    # A test that reached those would uninstall the user's own HUD — so point
    # every one of its paths at tmp_path here, not only in the tests that
    # exercise it.
    import claude_unlimited.hud as hud
    monkeypatch.setattr(hud, "BUNDLE_DIR", tmp_path / "Applications")
    monkeypatch.setattr(hud, "LEGACY_BUNDLE", tmp_path / "Applications" / "CapacityWidget.app")
    monkeypatch.setattr(hud, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / f"{hud.LABEL}.plist")
    monkeypatch.setattr(hud, "STAMP", tmp_path / "hud-version")
    monkeypatch.setattr(hud, "OPT_OUT", tmp_path / "hud-removed")
    monkeypatch.setattr(hud, "APP_DIR", app)


@pytest.fixture(autouse=True)
def no_real_usage_endpoint_calls(monkeypatch):
    """usage_probe's single network seam refuses by default; a test that
    exercises the HTTP layer installs its own fake."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach a real usage endpoint")
    monkeypatch.setattr(usage_probe, "_urlopen", refuse)


@pytest.fixture(autouse=True)
def no_background_threads_outliving_a_test(monkeypatch):
    """A thread started inside a test must not outlive it.

    Gateway schedules credential checks on a background thread. When a test
    ended before that thread did, monkeypatch had already restored the real
    config path — and the thread wrote its test pool over the developer's four
    live Profiles. Inside the suite the checks run inline instead, on the
    caller's thread, where the redirected paths still apply.
    """
    import claude_unlimited.gateway as gateway

    def run_inline(self, pool):
        try:
            self.run_credential_checks_now(pool)
        except Exception:
            pass

    monkeypatch.setattr(gateway.Gateway, "_schedule_credential_checks", run_inline)


@pytest.fixture(autouse=True)
def no_real_process_signals(monkeypatch):
    """The suite must never find or signal a real process.

    cli._stop_running_daemon() falls back to whoever holds the port, via a
    real `lsof`. One test reached that fallback unmocked, found the
    developer's live daemon on 4317, and SIGTERMed it on every run (launchd
    quietly restarted it). The discovery seam now finds nothing by default;
    a test that needs a stray pid installs its own. The pid-file route is
    closed by no_real_home below."""
    import claude_unlimited.cli as cli
    monkeypatch.setattr(cli, "_pids_listening_on", lambda port: [])


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch, tmp_path):
    """Path.home() must never be the developer's home during a test.

    cli._stop_running_daemon() reads ~/.claude-unlimited/daemon.pid and
    signals that pid; a test reaching it without patching Path.home would
    SIGTERM the live daemon (the lsof fallback above was one such route).
    Path.home() follows $HOME, so pointing $HOME at an empty directory closes
    every route at once. Tests that patch cli.Path.home still win."""
    home = tmp_path / "_isolated_home"   # not "home": tests create their own tmp_path/"home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

