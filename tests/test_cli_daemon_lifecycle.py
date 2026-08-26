import errno

import pytest

import claude_unlimited.cli as cli
import claude_unlimited.daemon_installer as daemon_installer


def test_start_when_already_running_is_a_friendly_noop(monkeypatch, capsys):
    # A second `claude-unlimited start` must recognize its own daemon and exit
    # cleanly, not fail to bind the port.
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    calls = []
    monkeypatch.setattr(cli, "run_foreground", lambda **kw: calls.append(kw))
    assert cli.main(["start"]) == 0
    assert calls == []  # never even tried to bind
    out = capsys.readouterr().out
    assert "Already running" in out


def test_start_port_in_use_by_something_else_is_a_friendly_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: False)

    def boom(**kw):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(cli, "run_foreground", boom)
    assert cli.main(["start"]) == 1
    err = capsys.readouterr().err
    assert "already in use" in err.lower()
    assert "Traceback" not in err


def test_start_reraises_unrelated_os_errors(monkeypatch):
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: False)

    def boom(**kw):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(cli, "run_foreground", boom)
    with pytest.raises(OSError):
        cli.main(["start"])


def test_code_missing_claude_binary_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["code"]) == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_code_when_daemon_already_running_execs_claude_directly(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-123")
    spawn_calls = []
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: spawn_calls.append(port))
    exec_calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: exec_calls.append((cmd, args)))

    assert cli.main(["code", "--model", "opus"]) == 0

    assert spawn_calls == []  # already running — never tried to start a second one
    # A --settings status-line hint may be prepended (see cli._status_line_args);
    # what matters is that claude is exec'd with the user's own args intact.
    (binary, argv), = exec_calls
    assert binary == "claude"
    assert argv[0] == "claude" and argv[-2:] == ["--model", "opus"]
    assert cli.os.environ["ANTHROPIC_BASE_URL"] == f"http://{cli.LOOPBACK_HOST}:{cli.DEFAULT_PORT}"
    assert cli.os.environ["ANTHROPIC_AUTH_TOKEN"] == "tok-123"


def test_code_starts_daemon_when_not_running(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    probe_calls = []

    def fake_probe(host, port, timeout=1.0):
        probe_calls.append(1)
        return len(probe_calls) > 1  # down on the first check, up from then on

    monkeypatch.setattr(cli, "_probe_health", fake_probe)
    spawn_calls = []
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: spawn_calls.append(port))
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok-456")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    exec_calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: exec_calls.append((cmd, args)))

    assert cli.main(["code"]) == 0

    assert spawn_calls == [cli.DEFAULT_PORT]
    (binary, argv), = exec_calls
    assert binary == "claude"
    assert argv[0] == "claude"


def test_code_gives_up_if_daemon_never_comes_up(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: False)
    monkeypatch.setattr(cli, "_spawn_background_daemon", lambda port: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert cli.main(["code"]) == 1
    assert "didn't come up" in capsys.readouterr().err


def test_code_custom_port_is_forwarded_everywhere(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: port == 5000)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok")
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: None)

    assert cli.main(["code", "--port", "5000"]) == 0
    assert cli.os.environ["ANTHROPIC_BASE_URL"] == f"http://{cli.LOOPBACK_HOST}:5000"


def test_status_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Not installed" in out


def test_status_installed_and_running(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 999})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "999" in out
    assert "running" in out.lower()


def test_status_installed_not_running(monkeypatch, capsys):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": False, "pid": None})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "not currently running" in out.lower()


def test_install_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    assert cli.main(["install", "--port", "5000"]) == 0
    assert calls == [5000]
    assert "Installed" in capsys.readouterr().out


def test_install_default_port(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    assert cli.main(["install"]) == 0
    assert calls == [cli.DEFAULT_PORT]


def test_install_failure_returns_nonzero(monkeypatch, capsys):
    def boom(port):
        raise daemon_installer.DaemonInstallerError("launchctl exploded")

    monkeypatch.setattr(daemon_installer, "install", boom)
    assert cli.main(["install"]) == 1
    assert "launchctl exploded" in capsys.readouterr().err


def test_uninstall_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "uninstall", lambda: calls.append(True))
    assert cli.main(["uninstall"]) == 0
    assert calls == [True]
    assert "Uninstalled" in capsys.readouterr().out


def test_uninstall_failure_returns_nonzero(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("nope")

    monkeypatch.setattr(daemon_installer, "uninstall", boom)
    assert cli.main(["uninstall"]) == 1


def test_service_start_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "start", lambda: calls.append(True))
    assert cli.main(["service-start"]) == 0
    assert calls == [True]
    assert "Started" in capsys.readouterr().out


def test_service_start_failure(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("not installed")

    monkeypatch.setattr(daemon_installer, "start", boom)
    assert cli.main(["service-start"]) == 1


def test_service_stop_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_installer, "stop", lambda: calls.append(True))
    assert cli.main(["service-stop"]) == 0
    assert calls == [True]
    assert "Stopped" in capsys.readouterr().out


def test_service_stop_failure(monkeypatch, capsys):
    def boom():
        raise daemon_installer.DaemonInstallerError("not installed")

    monkeypatch.setattr(daemon_installer, "stop", boom)
    assert cli.main(["service-stop"]) == 1


def test_purge_refuses_without_confirmation_when_not_interactive(monkeypatch, capsys):
    """Deleting credentials and config is irreversible, so a piped invocation
    must not proceed silently."""
    from claude_unlimited import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.purge(assume_yes=False) == 1
    assert "Refusing to purge" in capsys.readouterr().err


def test_purge_deletes_credentials_before_removing_the_config(monkeypatch, tmp_path):
    """The config is the only record of which Profiles exist. Removing it
    first would strand their credentials in the keystore with no way left to
    enumerate them."""
    from claude_unlimited import cli
    from claude_unlimited.config import Pool, Profile

    order = []
    deleted = []

    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[
        Profile(id="p1", name="A", kind="oauth", priority=1, automatic=True, enabled=True, account_uuid="u"),
        Profile(id="p2", name="B", kind="api", priority=2, automatic=True, enabled=True),
    ]))
    import claude_unlimited.secret_store as store
    monkeypatch.setattr(store, "delete_token", lambda pid: (order.append("delete_token"), deleted.append(pid)))

    real_rmtree = cli.shutil.rmtree
    monkeypatch.setattr(cli.shutil, "rmtree",
                        lambda p, **k: order.append("rmtree"))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude-unlimited").mkdir()
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: None)

    assert cli.purge(assume_yes=True) == 0
    assert deleted == ["p1", "p2"]
    assert order.index("delete_token") < order.index("rmtree")


def test_purge_never_touches_the_users_claude_directory(monkeypatch, tmp_path):
    from claude_unlimited import cli
    from claude_unlimited.config import Pool

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("mine")
    (tmp_path / ".claude-unlimited").mkdir()

    monkeypatch.setattr(cli, "load_pool", lambda: Pool(profiles=[]))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli.daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(cli.daemon_installer, "uninstall", lambda: None)

    assert cli.purge(assume_yes=True) == 0
    assert (claude_dir / "CLAUDE.md").read_text() == "mine"
    assert not (tmp_path / ".claude-unlimited").exists()
