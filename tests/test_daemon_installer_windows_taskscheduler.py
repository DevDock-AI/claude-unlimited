"""Command-shape and pidfile-logic tests only.

These lock in which schtasks commands get run; they do not exercise a real
Task Scheduler. See windows_taskscheduler.py's module docstring.
"""

from unittest.mock import MagicMock, patch

import pytest

from claude_unlimited.daemon_installer import windows_taskscheduler as backend


def _ok(stdout="", returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def isolated_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "PID_FILE", tmp_path / "daemon.pid")
    return tmp_path


def test_is_installed_reflects_schtasks_query_exit_code():
    with patch.object(backend, "_run", return_value=_ok(returncode=1)):
        assert backend.is_installed() is False
    with patch.object(backend, "_run", return_value=_ok(returncode=0)):
        assert backend.is_installed() is True


def test_install_creates_task_and_starts_it():
    calls = []

    def fake_run(*args):
        calls.append(args)
        return _ok()

    with patch.object(backend, "_run", side_effect=fake_run), \
         patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_stop_running_instance"):
        backend.install(4317)

    create_call = calls[0]
    assert create_call[:3] == ("schtasks", "/create", "/tn")
    assert backend.TASK_NAME in create_call
    assert any("claude_unlimited start --port 4317" in a for a in create_call)
    assert ("schtasks", "/run", "/tn", backend.TASK_NAME) in calls


def test_install_raises_on_create_failure():
    with patch.object(backend, "_run", return_value=_ok(returncode=1, stdout="access denied")):
        with pytest.raises(backend.DaemonInstallerError):
            backend.install(4317)


def test_status_not_installed():
    with patch.object(backend, "is_installed", return_value=False):
        assert backend.status() == {"installed": False, "running": False, "pid": None}


def test_status_running_when_pidfile_matches_a_live_process():
    backend.PID_FILE.write_text("4242")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True):
        assert backend.status() == {"installed": True, "running": True, "pid": 4242}


def test_status_not_running_when_pidfile_is_stale():
    backend.PID_FILE.write_text("4242")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=False):
        assert backend.status() == {"installed": True, "running": False, "pid": None}


def test_status_not_running_when_no_pidfile():
    with patch.object(backend, "is_installed", return_value=True):
        assert backend.status() == {"installed": True, "running": False, "pid": None}


def test_stop_requires_installed():
    with patch.object(backend, "is_installed", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.stop()


def test_stop_raises_when_no_live_process_found():
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.stop()


def test_stop_taskkills_the_pid_from_the_pidfile():
    backend.PID_FILE.write_text("4242")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True), \
         patch.object(backend, "_run", return_value=_ok()) as mock_run:
        backend.stop()
    mock_run.assert_called_once_with("taskkill", "/pid", "4242", "/f")


def test_start_requires_installed():
    with patch.object(backend, "is_installed", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.start()
