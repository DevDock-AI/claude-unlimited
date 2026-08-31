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

    # start() now hands the actual (re)start to a detached helper — the daemon
    # can't taskkill-then-run itself — so we assert install created the task and
    # delegated the start, rather than expecting an inline schtasks /run.
    with patch.object(backend, "_run", side_effect=fake_run), \
         patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_detached_restart") as mock_restart:
        backend.install(4317)

    create_call = calls[0]
    assert create_call[:3] == ("schtasks", "/create", "/tn")
    assert backend.TASK_NAME in create_call
    assert any("claude_unlimited start --port 4317" in a for a in create_call)
    mock_restart.assert_called_once()


def test_start_delegates_to_a_detached_restart():
    """The process calling start() may be the daemon itself; the restart must
    outlive it, so start() spawns a detached helper rather than killing itself
    inline."""
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_detached_restart") as mock_restart:
        backend.start()
    mock_restart.assert_called_once()


def test_detached_restart_spawns_a_surviving_process():
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend.subprocess, "Popen") as mock_popen:
        backend._detached_restart()
    mock_popen.assert_called_once()
    # The helper script must reference both the kill and the task start.
    script = mock_popen.call_args[0][0][-1]
    assert "taskkill" in script and "schtasks" in script and backend.TASK_NAME in script


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
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True), \
         patch.object(backend, "_pid_is_ours", return_value=True), \
         patch.object(backend, "_run", return_value=_ok()) as mock_run:
        backend.stop()
    mock_run.assert_called_once_with("taskkill", "/pid", "4242", "/f")


def test_stop_refuses_to_kill_a_recycled_pid_that_is_not_ours():
    """Windows reuses pids aggressively and our pidfile goes stale on unclean
    shutdown — force-killing whatever inherited the number would be a bug."""
    backend.PID_FILE.write_text("4242", encoding="utf-8")
    with patch.object(backend, "is_installed", return_value=True), \
         patch.object(backend, "_pid_is_alive", return_value=True), \
         patch.object(backend, "_pid_is_ours", return_value=False), \
         patch.object(backend, "_run", return_value=_ok()) as mock_run:
        with pytest.raises(backend.DaemonInstallerError, match="recycled"):
            backend.stop()
    mock_run.assert_not_called()


def test_pid_is_ours_requires_a_python_image():
    with patch.object(backend, "_tasklist_line",
                      return_value="python.exe                   4242 Console   1   50,000 K\n"):
        assert backend._pid_is_ours(4242) is True
    with patch.object(backend, "_tasklist_line",
                      return_value="explorer.exe                 4242 Console   1   90,000 K\n"):
        assert backend._pid_is_ours(4242) is False


def test_start_requires_installed():
    with patch.object(backend, "is_installed", return_value=False):
        with pytest.raises(backend.DaemonInstallerError):
            backend.start()
