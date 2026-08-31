r"""`claude-unlimited desktop` on Windows.

The app's Windows layout is not a mirror of the macOS one and none of it is
guessed - it was read off a configured Windows 11 install:

  * the normal profile lives in %APPDATA%\Claude (Roaming), the 3p profile in
    %LOCALAPPDATA%\Claude-3p (Local), so the two do NOT share a parent;
  * the app ships as an MSIX package, which cannot be launched by running its
    .exe out of the protected WindowsApps directory;
  * the desktop app and the Claude Code CLI are both called claude.exe.

Nothing here touches the real system: PowerShell is stubbed everywhere.
"""
import claude_unlimited.cli as cli


def test_windows_profiles_do_not_share_a_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    one_p, three_p = cli._desktop_userdata_dirs()
    assert one_p == tmp_path / "Roaming" / "Claude"
    assert three_p == tmp_path / "Local" / "Claude-3p"
    assert one_p.parent != three_p.parent


def test_macos_layout_is_unchanged(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    one_p, three_p = cli._desktop_userdata_dirs()
    assert one_p == cli.CLAUDE_APP_SUPPORT / "Claude"
    assert three_p == cli.CLAUDE_APP_SUPPORT / "Claude-3p"


def test_restore_maps_backup_names_without_a_shared_parent(monkeypatch, tmp_path):
    """Restoring used to rebuild paths as CLAUDE_APP_SUPPORT / name, which on
    Windows would write both profiles into the wrong root."""
    one_p, three_p = tmp_path / "Roaming" / "Claude", tmp_path / "Local" / "Claude-3p"
    monkeypatch.setattr(cli, "CLAUDE_1P_DIR", one_p)
    monkeypatch.setattr(cli, "CLAUDE_3P_DIR", three_p)

    assert cli._desktop_dir_named("Claude") == one_p
    assert cli._desktop_dir_named("Claude-3p") == three_p
    assert cli._desktop_dir_named("Something-Else") is None

    backup = tmp_path / "backup"
    (backup / "Claude").mkdir(parents=True)
    (backup / "Claude-3p").mkdir(parents=True)
    (backup / "Claude" / "claude_desktop_config.json").write_text('{"a": 1}', encoding="utf-8")
    (backup / "Claude-3p" / "config.json").write_text('{"b": 2}', encoding="utf-8")

    assert cli._restore_desktop_backup(backup) == 2
    assert (one_p / "claude_desktop_config.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (three_p / "config.json").read_text(encoding="utf-8") == '{"b": 2}'


def test_the_cli_is_never_mistaken_for_the_desktop_app(monkeypatch):
    """Both are claude.exe. Quitting "Claude" by name would kill the person's
    own terminal session, so processes are matched on where they run from."""
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_powershell", lambda script, timeout=20.0: "\n".join([
        r"111|C:\Program Files\WindowsApps\Claude_1.0.0.0_arm64__publisherhash\app\Claude.exe",
        r"222|C:\Users\me\.local\bin\claude.exe",                       # the CLI
        r"333|C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.247\claude.exe",  # bundled CLI
        r"444|C:\Users\me\AppData\Local\AnthropicClaude\Claude.exe",    # non-MSIX install
        "555|",                                                          # no path readable
    ]))

    assert cli._windows_app_pids() == [111, 444]
    assert cli._desktop_app_running() is True


def test_quitting_asks_the_window_to_close_and_never_force_kills(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    scripts = []
    alive = {"pids": [111]}

    def fake_powershell(script, timeout=20.0):
        scripts.append(script)
        alive["pids"] = []      # the app honours the close request
        return ""

    monkeypatch.setattr(cli, "_powershell", fake_powershell)
    monkeypatch.setattr(cli, "_windows_app_pids", lambda: alive["pids"])
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    assert cli._quit_desktop_app(timeout=5) is True
    assert "CloseMainWindow" in scripts[0]
    # A force kill would skip the config rewrite this whole dance waits for.
    assert "taskkill" not in " ".join(scripts).lower()
    assert "/F" not in " ".join(scripts)


def test_quitting_reports_failure_when_the_app_stays_up(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_powershell", lambda script, timeout=20.0: "")
    monkeypatch.setattr(cli, "_windows_app_pids", lambda: [111])
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    assert cli._quit_desktop_app(timeout=0.1) is False


def test_msix_app_is_launched_through_the_appsfolder_identity(monkeypatch):
    """Running the .exe from WindowsApps directly is blocked; the Start menu
    identity is the supported way in."""
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_launch_target", lambda: ("aumid", "Claude_examplehash!Claude"))
    monkeypatch.setattr(cli, "_desktop_app_running", lambda: True)
    seen = {}

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: seen.setdefault("cmd", cmd) or R())

    ok, why = cli._launch_desktop_app()
    assert ok and why == ""
    assert seen["cmd"][0] == "explorer.exe"
    assert seen["cmd"][1] == r"shell:AppsFolder\Claude_examplehash!Claude"


def test_a_plain_exe_install_is_launched_directly(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_launch_target", lambda: ("exe", r"C:\Apps\Claude.exe"))
    monkeypatch.setattr(cli, "_desktop_app_running", lambda: True)
    seen = {}

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: seen.setdefault("cmd", cmd) or R())

    assert cli._launch_desktop_app()[0] is True
    assert seen["cmd"] == [r"C:\Apps\Claude.exe"]


def test_linux_still_refuses_rather_than_guessing(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli.main(["desktop"]) == 1
    assert "macOS and Windows" in capsys.readouterr().err
