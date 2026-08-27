import io
import json
import subprocess
from pathlib import Path

import pytest

from claude_unlimited import updater
from claude_unlimited.updater import Release, UpdateError


def _response(payload):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

        def geturl(self):
            return "https://api.github.com/whatever"

    return _Ctx()


def _opener_for(pages):
    """Serves canned JSON per URL. Fails loudly on an unexpected URL, so a
    test can never accidentally reach the network."""
    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for fragment, payload in pages.items():
            if fragment in url:
                return _response(payload)
        raise AssertionError(f"unexpected URL: {url}")
    return opener


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---- version comparison ----

@pytest.mark.parametrize("candidate,current,expected", [
    ("0.2.0", "0.1.0", True),
    ("v0.2.0", "0.1.0", True),
    ("0.1.0", "0.1.0", False),
    ("0.1.0", "0.2.0", False),
    ("1.0.0", "0.9.9", True),
    ("0.10.0", "0.9.0", True),      # numeric, not lexical
])
def test_is_newer(candidate, current, expected):
    assert updater.is_newer(candidate, current) is expected


def test_parse_version_stops_at_the_first_non_numeric_part():
    assert updater.parse_version("v1.2.3-rc1") == (1, 2, 3)


# ---- checking ----

def test_check_returns_none_when_already_current():
    opener = _opener_for({"releases/latest": {"tag_name": "v0.1.0"}})
    assert updater.check_for_update("0.1.0", opener=opener) is None


def test_check_resolves_the_tag_to_a_commit_sha():
    sha = "a" * 40
    opener = _opener_for({
        "releases/latest": {"tag_name": "v0.2.0", "body": "notes here"},
        "commits/v0.2.0": {"sha": sha},
    })
    release = updater.check_for_update("0.1.0", opener=opener)
    assert release == Release(version="0.2.0", tag="v0.2.0", commit_sha=sha, notes="notes here")


def test_check_rejects_an_unusable_sha():
    opener = _opener_for({
        "releases/latest": {"tag_name": "v0.2.0"},
        "commits/v0.2.0": {"sha": "not-a-sha"},
    })
    with pytest.raises(UpdateError, match="unusable commit SHA"):
        updater.check_for_update("0.1.0", opener=opener)


def test_check_surfaces_a_network_failure_rather_than_pretending_it_is_current():
    def opener(request, timeout=None):
        raise OSError("no route to host")
    with pytest.raises(UpdateError, match="Could not reach GitHub"):
        updater.check_for_update("0.1.0", opener=opener)


# ---- staging: the content verification ----

def test_stage_refuses_when_the_downloaded_commit_is_not_the_one_announced(tmp_path):
    """The whole point of the check: a tree whose contents were altered cannot
    produce the SHA the release named."""
    release = Release(version="0.2.0", tag="v0.2.0", commit_sha="a" * 40, notes="")
    dest = tmp_path / "staged"

    def runner(cmd, **kw):
        if cmd[1] == "clone":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return _completed()
        return _completed(stdout="b" * 40)  # a different commit than announced

    with pytest.raises(UpdateError, match="does not match"):
        updater.stage_release(release, dest, runner=runner)
    assert not dest.exists()  # nothing half-downloaded is left behind


def test_stage_accepts_a_matching_commit(tmp_path):
    sha = "c" * 40
    release = Release(version="0.2.0", tag="v0.2.0", commit_sha=sha, notes="")
    dest = tmp_path / "staged"

    def runner(cmd, **kw):
        if cmd[1] == "clone":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return _completed()
        return _completed(stdout=sha + "\n")

    assert updater.stage_release(release, dest, runner=runner) == dest


def test_stage_reports_a_failed_clone(tmp_path):
    release = Release(version="0.2.0", tag="v0.2.0", commit_sha="d" * 40, notes="")

    def runner(cmd, **kw):
        return _completed(returncode=128, stderr="fatal: not found")

    with pytest.raises(UpdateError, match="Could not download"):
        updater.stage_release(release, tmp_path / "staged", runner=runner)


# ---- installing: rollback safety ----

def _prepare(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "pyproject.toml").write_text("")
    (staged / "marker.txt").write_text("new")
    app = tmp_path / "app"
    app.mkdir()
    (app / "marker.txt").write_text("old")
    venv = tmp_path / "venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    return staged, app, tmp_path / "app.previous", venv


def test_install_replaces_the_app_and_keeps_the_previous_copy(tmp_path):
    staged, app, previous, venv = _prepare(tmp_path)
    updater.install_staged(staged, runner=lambda *a, **k: _completed(),
                            app_dir=app, previous_dir=previous, venv_python=venv)
    assert (app / "marker.txt").read_text() == "new"
    assert (previous / "marker.txt").read_text() == "old"


def test_install_rolls_back_when_the_new_version_cannot_be_imported(tmp_path):
    staged, app, previous, venv = _prepare(tmp_path)

    def runner(cmd, **kw):
        if cmd[1:3] == ["-c", "import claude_unlimited"]:
            return _completed(returncode=1, stderr="ImportError: boom")
        return _completed()

    with pytest.raises(UpdateError, match="could not be imported, rolled back"):
        updater.install_staged(staged, runner=runner, app_dir=app,
                                previous_dir=previous, venv_python=venv)
    assert (app / "marker.txt").read_text() == "old"  # the working version is back


def test_install_rolls_back_when_pip_fails(tmp_path):
    staged, app, previous, venv = _prepare(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if "install" in cmd and str(app) in cmd and len(calls) == 1:
            return _completed(returncode=1, stderr="pip exploded")
        return _completed()

    with pytest.raises(UpdateError, match="rolled back"):
        updater.install_staged(staged, runner=runner, app_dir=app,
                                previous_dir=previous, venv_python=venv)
    assert (app / "marker.txt").read_text() == "old"


def test_install_refuses_a_tree_that_is_not_this_project(tmp_path):
    staged, app, previous, venv = _prepare(tmp_path)
    (staged / "pyproject.toml").unlink()
    with pytest.raises(UpdateError, match="does not look like this project"):
        updater.install_staged(staged, runner=lambda *a, **k: _completed(),
                                app_dir=app, previous_dir=previous, venv_python=venv)
    assert (app / "marker.txt").read_text() == "old"


def test_install_refuses_without_a_virtualenv(tmp_path):
    staged, app, previous, venv = _prepare(tmp_path)
    venv.unlink()
    with pytest.raises(UpdateError, match="No virtual environment"):
        updater.install_staged(staged, runner=lambda *a, **k: _completed(),
                                app_dir=app, previous_dir=previous, venv_python=venv)


# ---- the source cannot be redirected ----

def test_update_source_is_hardcoded_not_configurable():
    """A compromised config file must not be able to point the updater at a
    different repository."""
    assert updater.CLONE_URL == "https://github.com/DevDock-AI/claude-unlimited.git"
    assert updater.RELEASES_LATEST_URL.startswith("https://api.github.com/repos/DevDock-AI/claude-unlimited/")
    source = Path(updater.__file__).read_text()
    assert "load_pool" not in source and "update_settings" not in source


# ---- mode policy ----

def _cycle_env(tmp_path, sha="e" * 40):
    pages = {"releases/latest": {"tag_name": "v0.2.0", "body": "n"}, "commits/v0.2.0": {"sha": sha}}
    staged = tmp_path / "staged"

    def runner(cmd, **kw):
        if cmd[1] == "clone":
            d = Path(cmd[-1]); d.mkdir(parents=True, exist_ok=True)
            (d / "pyproject.toml").write_text("")
            return _completed()
        if cmd[1] == "-C":
            return _completed(stdout=sha)
        return _completed()
    return _opener_for(pages), runner, staged


def test_manual_mode_reports_but_downloads_nothing(tmp_path):
    opener, runner, staged = _cycle_env(tmp_path)
    out = updater.run_update_cycle("0.1.0", "manual", opener=opener, runner=runner, staging_dir=staged)
    assert out.action == "available" and out.release.version == "0.2.0"
    assert not staged.exists()
    assert out.needs_restart is False


def test_auto_download_mode_stages_but_does_not_install(tmp_path, monkeypatch):
    opener, runner, staged = _cycle_env(tmp_path)
    monkeypatch.setattr(updater, "install_staged",
                        lambda *a, **k: pytest.fail("must not install in auto_download mode"))
    out = updater.run_update_cycle("0.1.0", "auto_download", opener=opener, runner=runner, staging_dir=staged)
    assert out.action == "downloaded"
    assert staged.exists()


def test_auto_install_mode_installs(tmp_path, monkeypatch):
    opener, runner, staged = _cycle_env(tmp_path)
    installed = []
    monkeypatch.setattr(updater, "install_staged", lambda s, **k: installed.append(s))
    out = updater.run_update_cycle("0.1.0", "auto_install", opener=opener, runner=runner, staging_dir=staged)
    assert out.action == "installed" and out.needs_restart is True
    assert installed


def test_cycle_never_raises_on_a_network_failure(tmp_path):
    def opener(request, timeout=None):
        raise OSError("offline")
    out = updater.run_update_cycle("0.1.0", "auto_install", opener=opener, staging_dir=tmp_path / "s")
    assert out.action == "none" and "Could not reach GitHub" in out.error


def test_a_failed_install_reports_downloaded_not_installed(tmp_path, monkeypatch):
    opener, runner, staged = _cycle_env(tmp_path)
    def boom(*a, **k): raise UpdateError("install blew up")
    monkeypatch.setattr(updater, "install_staged", boom)
    out = updater.run_update_cycle("0.1.0", "auto_install", opener=opener, runner=runner, staging_dir=staged)
    assert out.action == "downloaded" and "install blew up" in out.error
    assert out.needs_restart is False


def test_no_update_available_is_not_an_error(tmp_path):
    opener = _opener_for({"releases/latest": {"tag_name": "v0.1.0"}})
    out = updater.run_update_cycle("0.1.0", "auto_install", opener=opener, staging_dir=tmp_path / "s")
    assert out.action == "none" and out.error is None and out.release is None


def test_a_repo_with_no_releases_is_not_an_error(tmp_path):
    """`releases/latest` 404s until the first release exists. That is a normal
    state, not something to report as a failure."""
    import urllib.error

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    out = updater.run_update_cycle("0.1.0", "auto_install", opener=opener, staging_dir=tmp_path / "s")
    assert out.error is None and out.release is None
    # Not "none": that means "checked, you already have the newest release",
    # and the dashboard says exactly that. A repo with nothing published is a
    # different fact, and claiming the person is up to date would be false.
    assert out.action == "no_releases"


def test_other_http_errors_are_still_reported(tmp_path):
    import urllib.error

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

    out = updater.run_update_cycle("0.1.0", "manual", opener=opener, staging_dir=tmp_path / "s")
    assert out.action == "none" and "HTTP 503" in out.error


# ---- idle gating (daemon policy, not updater internals) ----

def test_install_is_held_back_while_the_pool_is_in_use(monkeypatch, tmp_path):
    """Installing swaps the running code and needs a restart, so it must never
    land mid-session. The download still happens; only applying it waits."""
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon_mod._gateway, "is_idle", lambda _s: False)

    seen = {}

    def fake_cycle(version, mode, **kw):
        seen["mode"] = mode
        return updater.UpdateOutcome(
            release=Release(version="9.9.9", tag="v9.9.9", commit_sha="f" * 40, notes=""),
            action="downloaded")

    monkeypatch.setattr(daemon_mod.updater, "run_update_cycle", fake_cycle)
    state = daemon_mod._run_update_check(_settings_with_mode("auto_install"))

    assert seen["mode"] == "auto_download", "must not install while busy"
    assert state["install_deferred_until_idle"] is True


def test_install_proceeds_when_idle(monkeypatch, tmp_path):
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon_mod._gateway, "is_idle", lambda _s: True)

    seen = {}

    def fake_cycle(version, mode, **kw):
        seen["mode"] = mode
        return updater.UpdateOutcome(release=None, action="none")

    monkeypatch.setattr(daemon_mod.updater, "run_update_cycle", fake_cycle)
    daemon_mod._run_update_check(_settings_with_mode("auto_install"))
    assert seen["mode"] == "auto_install"


def _settings_with_mode(mode):
    from claude_unlimited.config import Settings
    return Settings(update_mode=mode)


def test_check_response_carries_the_version_fields_the_ui_shows(monkeypatch, tmp_path):
    """The check response and the GET must be the same shape. When it wasn't,
    the Dashboard blanked the installed and latest versions after a check."""
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon_mod._gateway, "is_idle", lambda _s: True)
    monkeypatch.setattr(daemon_mod.updater, "run_update_cycle",
                        lambda *a, **k: updater.UpdateOutcome(release=None, action="none"))

    checked = daemon_mod._run_update_check(_settings_with_mode("manual"))
    served = daemon_mod._public_update_state(_settings_with_mode("manual"))

    for field in ("current_version", "update_mode", "available", "action", "checked_at"):
        assert field in checked, f"check response missing {field}"
    assert set(checked) == set(served), "check and GET must return the same keys"
    assert checked["current_version"] == daemon_mod.__version__


def test_check_now_never_downloads_or_installs(monkeypatch, tmp_path):
    """A button that says it is checking must not install software. The check
    endpoint pins the mode to manual regardless of the configured policy."""
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon_mod._gateway, "is_idle", lambda _s: True)

    modes = []

    def fake_cycle(version, mode, **kw):
        modes.append(mode)
        return updater.UpdateOutcome(release=None, action="none")

    monkeypatch.setattr(daemon_mod.updater, "run_update_cycle", fake_cycle)

    # Even with the most aggressive policy configured.
    daemon_mod._run_update_check(_settings_with_mode("auto_install"), mode_override="manual")
    assert modes == ["manual"], modes
    assert "auto_install" not in modes and "auto_download" not in modes


def test_background_policy_still_honours_the_configured_mode(monkeypatch, tmp_path):
    """The override is only for the explicit button — the background loop must
    still do what the user configured."""
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon_mod._gateway, "is_idle", lambda _s: True)

    modes = []
    monkeypatch.setattr(daemon_mod.updater, "run_update_cycle",
                        lambda v, mode, **k: (modes.append(mode),
                                              updater.UpdateOutcome(release=None, action="none"))[1])
    daemon_mod._run_update_check(_settings_with_mode("auto_install"))
    assert modes == ["auto_install"], modes


# ---- restarting after an install ----

def test_a_service_managed_daemon_is_bounced_by_the_service_manager(monkeypatch):
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod._gateway, "_persist", lambda: None)
    monkeypatch.setattr(daemon_mod.daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 1})
    started = []
    monkeypatch.setattr(daemon_mod.daemon_installer, "start", lambda: started.append(True))
    monkeypatch.setattr(daemon_mod.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not hand off when a service owns it"))

    assert daemon_mod._restart_for_update(4317) is True
    assert started


def test_a_detached_daemon_launches_a_replacement(monkeypatch):
    """install.sh starts the daemon detached, so no service manager will bring
    it back — without a hand-off the update installs and nothing restarts."""
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod._gateway, "_persist", lambda: None)
    monkeypatch.setattr(daemon_mod.daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    spawned = []
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda cmd, **k: spawned.append(cmd))

    timers = []
    class _T:
        def __init__(self, *a, **k): timers.append(a)
        def start(self): pass
    monkeypatch.setattr(daemon_mod.threading, "Timer", _T)

    assert daemon_mod._restart_for_update(4317) is True
    assert spawned, "a replacement must be launched"
    assert "claude_unlimited" in " ".join(spawned[0]) or "-c" in spawned[0]
    assert timers, "the current process must be told to exit"


def test_a_failed_restart_is_recorded_not_swallowed(monkeypatch):
    import claude_unlimited.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod._gateway, "_persist", lambda: None)
    monkeypatch.setattr(daemon_mod.daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 1})

    def boom():
        raise daemon_mod.daemon_installer.DaemonInstallerError("nope")

    monkeypatch.setattr(daemon_mod.daemon_installer, "start", boom)
    recorded = []
    monkeypatch.setattr(daemon_mod.activity, "record", lambda *a, **k: recorded.append(a))

    assert daemon_mod._restart_for_update(4317) is False
    assert recorded and "restart failed" in recorded[0][1]


def test_state_is_snapshotted_before_the_process_goes_away(monkeypatch):
    """The replacement reads this back, so skipping it means the Dashboard
    returns blank after every update."""
    import claude_unlimited.daemon as daemon_mod

    order = []
    monkeypatch.setattr(daemon_mod._gateway, "_persist", lambda: order.append("persist"))
    monkeypatch.setattr(daemon_mod.daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 1})
    monkeypatch.setattr(daemon_mod.daemon_installer, "start", lambda: order.append("restart"))

    daemon_mod._restart_for_update(4317)
    assert order == ["persist", "restart"]


def test_recording_a_no_releases_outcome_notifies_nothing_and_does_not_crash():
    """`_record_update_outcome` dereferences outcome.release for anything it
    treats as newsworthy. "no_releases" carries no Release, so mistaking it for
    an actionable outcome is an AttributeError in a background loop."""
    from claude_unlimited import daemon

    outcome = updater.UpdateOutcome(release=None, action="no_releases")
    daemon._record_update_outcome(outcome, None)

    assert daemon._update_state["action"] == "no_releases"
    assert daemon._update_state["available"] is None
    assert daemon._update_state["error"] is None
