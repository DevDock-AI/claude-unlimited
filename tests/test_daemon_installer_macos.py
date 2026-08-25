import subprocess

import pytest

import claude_unlimited.daemon_installer.macos_launchd as launchd


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(launchd, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "LaunchAgents" / f"{launchd.LABEL}.plist")
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "logs")
    return tmp_path


class FakeLaunchctl:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, *args):
        self.calls.append(args)
        for prefix, result in self.responses.items():
            if args[: len(prefix)] == prefix:
                return result
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_is_installed_false_before_install(isolated_paths):
    assert launchd.is_installed() is False


def test_install_writes_plist_and_bootstraps(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)

    launchd.install(port=4317)

    assert launchd.is_installed() is True
    assert launchd.LOG_DIR.exists()
    assert any(c[0] == "bootstrap" for c in fake.calls)
    assert any(c[0] == "bootout" for c in fake.calls)  # unload-first for idempotent re-install

    import plistlib

    plist = plistlib.loads(launchd.PLIST_PATH.read_bytes())
    assert plist["Label"] == launchd.LABEL
    assert plist["ProgramArguments"][-2:] == ["--port", "4317"]
    assert plist["RunAtLoad"] is True


def test_install_raises_on_bootstrap_failure(isolated_paths, monkeypatch):
    fail = subprocess.CompletedProcess(["launchctl"], 1, stdout="", stderr="boom")
    fake = FakeLaunchctl(responses={("bootstrap",): fail})
    monkeypatch.setattr(launchd, "_run_launchctl", fake)

    with pytest.raises(launchd.DaemonInstallerError, match="boom"):
        launchd.install(port=4317)


def test_uninstall_removes_plist_and_boots_out(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    assert launchd.is_installed() is True

    launchd.uninstall()
    assert launchd.is_installed() is False
    assert any(c[0] == "bootout" for c in fake.calls)


def test_uninstall_when_never_installed_does_not_raise(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.uninstall()  # must not raise


def test_status_not_installed(isolated_paths):
    assert launchd.status() == {"installed": False, "running": False, "pid": None}


def test_status_installed_but_not_running(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)

    not_running = subprocess.CompletedProcess(["launchctl"], 0, stdout="state = not running\n", stderr="")
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl(responses={("print",): not_running}))
    result = launchd.status()
    assert result == {"installed": True, "running": False, "pid": None}


def test_status_installed_and_running_parses_pid(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)

    running = subprocess.CompletedProcess(["launchctl"], 0, stdout="state = running\n\tpid = 12345\n", stderr="")
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl(responses={("print",): running}))
    result = launchd.status()
    assert result == {"installed": True, "running": True, "pid": 12345}


def test_start_requires_install(isolated_paths, monkeypatch):
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl())
    with pytest.raises(launchd.DaemonInstallerError, match="install"):
        launchd.start()


def test_start_kickstarts_when_installed(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.start()
    assert any(c[0] == "kickstart" for c in fake.calls)


def test_stop_requires_install(isolated_paths, monkeypatch):
    monkeypatch.setattr(launchd, "_run_launchctl", FakeLaunchctl())
    with pytest.raises(launchd.DaemonInstallerError, match="install|Not installed"):
        launchd.stop()


def test_stop_kills_when_installed(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.stop()
    assert any(c[0] == "kill" for c in fake.calls)


def test_reinstall_with_different_port_updates_plist(isolated_paths, monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run_launchctl", fake)
    launchd.install(port=4317)
    launchd.install(port=5000)

    import plistlib

    plist = plistlib.loads(launchd.PLIST_PATH.read_bytes())
    assert plist["ProgramArguments"][-2:] == ["--port", "5000"]
