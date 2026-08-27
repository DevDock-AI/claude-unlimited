"""`claude-unlimited desktop` — configure the Claude desktop app to use the pool.

The app calls this third-party ("3p") inference mode. It runs from a SEPARATE
userData directory with its own settings and bundled Claude Code, which is why
none of this lives in the normal profile.

Schema here is not invented: it was read back out of the app after configuring
it by hand through Developer > Configure Third-Party Inference.
"""
import json

import claude_unlimited.cli as cli


def _dirs(monkeypatch, tmp_path):
    """Points every path at tmp_path. NEVER fake the filesystem here — a
    blanket Path.exists patch once wrote a fake token into the real app."""
    one_p, three_p = tmp_path / "Claude", tmp_path / "Claude-3p"
    monkeypatch.setattr(cli, "CLAUDE_APP_SUPPORT", tmp_path)
    monkeypatch.setattr(cli, "CLAUDE_1P_DIR", one_p)
    monkeypatch.setattr(cli, "CLAUDE_3P_DIR", three_p)
    monkeypatch.setattr(cli, "APP_DIR_PATH", lambda: tmp_path / "cu")
    return one_p, three_p


def _entry_for(three_p, name=cli.CU_CONFIG_NAME):
    meta = json.loads((three_p / "configLibrary" / "_meta.json").read_text())
    entry = next(e for e in meta["entries"] if e["name"] == name)
    cfg = json.loads((three_p / "configLibrary" / f"{entry['id']}.json").read_text())
    return meta, entry, cfg


def test_fresh_install_with_nothing_on_disk(monkeypatch, tmp_path):
    # Nothing exists: no profile directories, no config library, never signed in.
    one_p, three_p = _dirs(monkeypatch, tmp_path)
    assert not one_p.exists() and not three_p.exists()

    cli._configure_desktop_app(4317, "sk-cu-token")

    meta, entry, cfg = _entry_for(three_p)
    assert meta["appliedId"] == entry["id"]
    assert cfg == {
        "inferenceGatewayBaseUrl": "http://127.0.0.1:4317",
        "inferenceGatewayApiKey": "sk-cu-token",
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "static",
    }
    # The deployment switch must reach the 1p profile, or the next launch
    # opens the normal profile and none of this is used.
    assert json.loads((one_p / "claude_desktop_config.json").read_text())["deploymentMode"] == "3p"
    assert json.loads((one_p / "developer_settings.json").read_text())["allowDevTools"] is True


def test_existing_configuration_is_preserved(monkeypatch, tmp_path):
    # A fully configured app: other inference profiles, other settings, an
    # applied profile that is not ours. None of it may be destroyed.
    one_p, three_p = _dirs(monkeypatch, tmp_path)
    lib = three_p / "configLibrary"
    lib.mkdir(parents=True)
    (lib / "_meta.json").write_text(json.dumps({
        "appliedId": "aaaa-1111",
        "entries": [{"id": "aaaa-1111", "name": "Default"},
                    {"id": "bbbb-2222", "name": "Work gateway"}]}))
    (lib / "aaaa-1111.json").write_text('{"inferenceProvider": "gateway"}')
    (lib / "bbbb-2222.json").write_text('{"inferenceGatewayBaseUrl": "https://work.example"}')
    one_p.mkdir(parents=True)
    (one_p / "claude_desktop_config.json").write_text(
        json.dumps({"coworkUserFilesPath": "/Users/x/Claude", "preferences": {"theme": "dark"}}))

    cli._configure_desktop_app(4317, "tok")

    meta, _, _ = _entry_for(three_p)
    names = {e["name"] for e in meta["entries"]}
    assert {"Default", "Work gateway", cli.CU_CONFIG_NAME} <= names
    assert json.loads((lib / "bbbb-2222.json").read_text())["inferenceGatewayBaseUrl"] == "https://work.example"
    # Unrelated keys in the app's own config survive.
    cfg = json.loads((one_p / "claude_desktop_config.json").read_text())
    assert cfg["coworkUserFilesPath"] == "/Users/x/Claude"
    assert cfg["preferences"] == {"theme": "dark"}
    assert cfg["deploymentMode"] == "3p"


def test_rerunning_updates_in_place_rather_than_duplicating(monkeypatch, tmp_path):
    _dirs(monkeypatch, tmp_path)
    first = cli._configure_desktop_app(4317, "tok-1")
    second = cli._configure_desktop_app(4999, "tok-2")
    assert first == second, "a second run created a duplicate profile"

    _, _, cfg = _entry_for(tmp_path / "Claude-3p")
    assert cfg["inferenceGatewayApiKey"] == "tok-2"
    assert cfg["inferenceGatewayBaseUrl"] == "http://127.0.0.1:4999"


def test_a_corrupt_meta_file_does_not_crash(monkeypatch, tmp_path):
    _, three_p = _dirs(monkeypatch, tmp_path)
    lib = three_p / "configLibrary"
    lib.mkdir(parents=True)
    (lib / "_meta.json").write_text("{ this is not json")

    cli._configure_desktop_app(4317, "tok")   # must not raise
    meta, _, _ = _entry_for(three_p)
    assert meta["appliedId"]


# NOTE: this used to assert the command REFUSED while the app was running.
# It now quits the app, writes, and relaunches — see
# test_a_running_app_is_quit_before_anything_is_written for the ordering, which
# is the part that matters, and test_it_refuses_if_the_app_will_not_quit for
# the case where quitting fails.


def test_the_suite_never_touches_the_real_app_config(monkeypatch, tmp_path):
    # Regression guard: a test once wrote a fake token into the developer's
    # actual Claude desktop configuration.
    real = cli.Path.home() / "Library" / "Application Support" / "Claude-3p" / "configLibrary"
    before = sorted(p.name for p in real.glob("*.json")) if real.is_dir() else None

    _dirs(monkeypatch, tmp_path)
    cli._configure_desktop_app(4317, "tok-fake")

    after = sorted(p.name for p in real.glob("*.json")) if real.is_dir() else None
    assert after == before, "the suite modified the real Claude app config library"


def test_a_running_app_is_quit_before_anything_is_written(monkeypatch, tmp_path):
    # Order matters: the app rewrites its config on exit, so writing while it
    # runs would be clobbered by its own shutdown.
    _dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "exists", lambda self: True)
    monkeypatch.setattr(cli, "_ensure_daemon", lambda port: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok")
    monkeypatch.setattr(cli, "load_pool", lambda: type("P", (), {"profiles": []})())

    order = []
    running = {"v": True}

    def fake_quit(timeout=20.0):
        order.append("quit")
        running["v"] = False
        return True

    monkeypatch.setattr(cli, "_desktop_app_running", lambda: running["v"])
    monkeypatch.setattr(cli, "_quit_desktop_app", fake_quit)
    real_configure = cli._configure_desktop_app
    monkeypatch.setattr(cli, "_configure_desktop_app",
                        lambda port, token: order.append("write") or real_configure(port, token))

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **k: order.append("launch") or R())

    assert cli.main(["desktop"]) == 0
    assert order == ["quit", "write", "launch"], order


def test_it_refuses_if_the_app_will_not_quit(monkeypatch, tmp_path, capsys):
    _dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "exists", lambda self: True)
    monkeypatch.setattr(cli, "_desktop_app_running", lambda: True)
    monkeypatch.setattr(cli, "_quit_desktop_app", lambda timeout=20.0: False)
    wrote = []
    monkeypatch.setattr(cli, "_configure_desktop_app", lambda p, t: wrote.append(1))

    assert cli.main(["desktop"]) == 1
    assert wrote == [], "configuration was written despite the app still running"
    assert "still running" in capsys.readouterr().err


# --- purge must undo what `desktop` did ------------------------------------
#
# purge deletes ~/.claude-unlimited, which is where the pre-change snapshot
# lives. Without an explicit revert first, purge would leave the desktop app
# pointed at a gateway that no longer exists AND destroy the only copy of the
# settings that could bring it back.


def _seed_backup(tmp_path, contents="{}\n"):
    backup = tmp_path / "cu" / "claude-desktop-backup" / "Claude-3p"
    backup.mkdir(parents=True)
    (backup / "claude_desktop_config.json").write_text(contents)
    return backup


def test_purge_restores_the_desktop_config_before_deleting_the_backup(monkeypatch, tmp_path):
    _dirs(monkeypatch, tmp_path)
    _seed_backup(tmp_path, '{"original": true}\n')
    three_p = tmp_path / "Claude-3p"
    three_p.mkdir(parents=True, exist_ok=True)
    (three_p / "claude_desktop_config.json").write_text('{"pointed_at": "claude-unlimited"}\n')
    monkeypatch.setattr(cli, "_desktop_app_running", lambda: False)

    cli._revert_desktop_config_for_purge()

    assert json.loads((three_p / "claude_desktop_config.json").read_text()) == {"original": True}


def test_purge_preserves_the_backup_when_the_app_will_not_quit(monkeypatch, tmp_path, capsys):
    _dirs(monkeypatch, tmp_path)
    _seed_backup(tmp_path, '{"original": true}\n')
    monkeypatch.setattr(cli, "_desktop_app_running", lambda: True)
    monkeypatch.setattr(cli, "_quit_desktop_app", lambda timeout=20.0: False)
    keep = tmp_path / "home" / "claude-unlimited-desktop-backup"
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()

    cli._revert_desktop_config_for_purge()

    saved = keep / "Claude-3p" / "claude_desktop_config.json"
    assert saved.is_file(), "the only restorable snapshot was left inside the doomed app dir"
    assert json.loads(saved.read_text()) == {"original": True}
    assert str(keep) in capsys.readouterr().err


def test_purge_is_a_no_op_when_the_desktop_app_was_never_configured(monkeypatch, tmp_path, capsys):
    _dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_desktop_app_running",
                        lambda: (_ for _ in ()).throw(AssertionError("probed the app pointlessly")))

    cli._revert_desktop_config_for_purge()

    assert capsys.readouterr().out == ""
