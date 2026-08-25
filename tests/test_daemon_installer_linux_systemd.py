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
    return tmp_path


def test_install_writes_unit_file_and_enables_now():
    with patch.object(backend, "_run_systemctl", return_value=_ok()) as mock_run:
        backend.install(4317)

    assert backend.UNIT_PATH.exists()
    contents = backend.UNIT_PATH.read_text()
    assert "claude_unlimited start --port 4317" in contents
    assert "[Install]" in contents

    calls = [c.args for c in mock_run.call_args_list]
    assert ("daemon-reload",) in calls
    assert ("enable", "--now", backend.UNIT_NAME) in calls


def test_install_raises_on_systemctl_failure():
    with patch.object(backend, "_run_systemctl", return_value=_ok(returncode=1, stdout="boom")):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)


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
