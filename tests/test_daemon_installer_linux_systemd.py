"""Command-shape and pure-logic tests only.

These lock in which systemd commands get run; they do not exercise a real
systemd user session. See linux_systemd.py's module docstring.
"""

from unittest.mock import MagicMock, patch

import pytest

from claude_unlimited.daemon_installer import linux_systemd as backend


def _ok(stdout="", returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def isolated_unit_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "UNIT_DIR", tmp_path / "systemd-user")
    monkeypatch.setattr(backend, "UNIT_PATH", tmp_path / "systemd-user" / backend.UNIT_NAME)
    monkeypatch.setattr(backend, "LOG_DIR", tmp_path / "logs")
    # Never touch a real loginctl or systemd version during these tests.
    monkeypatch.setattr(backend, "_enable_linger", lambda: None)
    monkeypatch.setattr(backend, "_systemd_version", lambda: 249)
    return tmp_path


def test_install_writes_unit_file_enables_and_restarts():
    with patch.object(backend, "_run_systemctl", return_value=_ok()) as mock_run:
        backend.install(4317)

    assert backend.UNIT_PATH.exists()
    contents = backend.UNIT_PATH.read_text()
    assert "claude_unlimited start --port 4317" in contents
    assert "[Install]" in contents
    # The crash-loop protection must be present, or a fast-failing daemon gets
    # permanently disabled by systemd's default start limit.
    assert "StartLimitIntervalSec=0" in contents

    calls = [c.args for c in mock_run.call_args_list]
    assert ("daemon-reload",) in calls
    assert ("enable", backend.UNIT_NAME) in calls
    # restart, NOT `enable --now`: a re-install to change the port must actually
    # replace the running process, which `start` on an active unit does not do.
    assert ("restart", backend.UNIT_NAME) in calls
    assert ("enable", "--now", backend.UNIT_NAME) not in calls


def test_install_raises_on_systemctl_failure_and_cleans_up_the_unit():
    def selective(*args):
        if args and args[0] == "restart":
            return _ok(returncode=1, stdout="boom")
        return _ok()

    with patch.object(backend, "_run_systemctl", side_effect=selective):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)
    # A failed install must not leave an orphan unit that then makes every
    # later status()/doctor() misreport "installed".
    assert not backend.UNIT_PATH.exists()


def test_install_on_a_systemd_less_box_raises_cleanly_and_leaves_no_unit():
    # _run_systemctl turns a missing `systemctl` into DaemonInstallerError; the
    # very first call (is-system-running) must abort before any unit is written.
    with patch.object(backend, "_run_systemctl",
                      side_effect=backend.DaemonInstallerError("no systemctl")):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)
    assert not backend.UNIT_PATH.exists()


def test_run_systemctl_maps_missing_binary_to_our_error(monkeypatch):
    monkeypatch.setattr(backend.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(backend.DaemonInstallerError, match="systemd"):
        backend._run_systemctl("status")


def test_is_installed_false_before_install_true_after():
    assert backend.is_installed() is False
    with patch.object(backend, "_run_systemctl", return_value=_ok()):
        backend.install(4317)
    assert backend.is_installed() is True


def test_uninstall_removes_unit_file():
    with patch.object(backend, "_run_systemctl", return_value=_ok()):
        backend.install(4317)
        assert backend.UNIT_PATH.exists()
        backend.uninstall()
    assert not backend.UNIT_PATH.exists()


def test_status_not_installed():
    assert backend.status() == {"installed": False, "running": False, "pid": None}


def test_status_parses_pid_and_active_state():
    with patch.object(backend, "_run_systemctl", return_value=_ok()):
        backend.install(4317)
    with patch.object(backend, "_run_systemctl",
                       return_value=_ok(stdout="MainPID=12345\nActiveState=active\n")):
        s = backend.status()
    assert s == {"installed": True, "running": True, "pid": 12345}


def test_status_not_running_when_inactive():
    with patch.object(backend, "_run_systemctl", return_value=_ok()):
        backend.install(4317)
    with patch.object(backend, "_run_systemctl",
                       return_value=_ok(stdout="MainPID=0\nActiveState=inactive\n")):
        s = backend.status()
    assert s == {"installed": True, "running": False, "pid": None}


def test_start_requires_installed():
    with pytest.raises(backend.DaemonInstallerError):
        backend.start()


def test_stop_requires_installed():
    with pytest.raises(backend.DaemonInstallerError):
        backend.stop()
