"""`claude-unlimited codex` (issue #7)."""

import os
import types

import pytest

import claude_unlimited.cli as cli
from claude_unlimited import codex_cli
from claude_unlimited.config import Pool, Profile, Settings


def _pool(enabled=True, profiles=None):
    return Pool(profiles=profiles if profiles is not None else [
        Profile(id="a", name="Work", kind="codex", auth_mode="chatgpt_subscription", automatic=True, enabled=True),
        Profile(id="c", name="Claude", kind="oauth", automatic=True, enabled=True),
    ], settings=Settings(codex_cli_serving=enabled))


@pytest.fixture
def launch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_ensure_daemon", lambda port: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda h, p: "placeholder")
    monkeypatch.setattr(cli, "_fetch_session_token", lambda h, p, pid: f"session-{pid}")
    monkeypatch.setattr(cli.os, "execvp", lambda cmd, args: calls.append(args))
    monkeypatch.setattr(cli, "_run_tool", lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    # setenv first so monkeypatch restores (removes) whatever cli.codex()
    # writes into os.environ; a bare delenv would not undo it.
    monkeypatch.setenv(codex_cli.TOKEN_ENV, "")
    monkeypatch.delenv(codex_cli.TOKEN_ENV)
    return calls


def test_launches_codex_pointed_at_the_daemon_with_its_own_args_untouched(monkeypatch, launch):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool())
    assert cli.main(["codex", "exec", "--model", "gpt-5.5", "hello"]) == 0
    argv = launch[0]
    assert argv[0] == "codex"
    assert argv[1:5] == codex_cli.launch_overrides(cli.DEFAULT_PORT)
    assert argv[5:] == ["exec", "--model", "gpt-5.5", "hello"]
    assert os.environ[codex_cli.TOKEN_ENV] == "placeholder"


def test_profile_pins_one_chatgpt_account(monkeypatch, launch):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool())
    assert cli.main(["codex", "--profile", "work"]) == 0
    assert os.environ[codex_cli.TOKEN_ENV] == "session-a"


def test_a_claude_account_cannot_be_pinned(monkeypatch, launch, capsys):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool())
    assert cli.main(["codex", "--profile", "Claude"]) == 1
    assert launch == []
    assert "Available: Work" in capsys.readouterr().err


def test_refuses_while_the_setting_is_off(monkeypatch, launch, capsys):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool(enabled=False))
    assert cli.main(["codex"]) == 1
    assert launch == []
    assert "turned off" in capsys.readouterr().err


def test_refuses_without_a_chatgpt_account(monkeypatch, launch, capsys):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool(profiles=[
        Profile(id="c", name="Claude", kind="oauth", automatic=True, enabled=True)]))
    assert cli.main(["codex"]) == 1
    assert "add-codex-account" in capsys.readouterr().err


def test_refuses_without_codex_installed(monkeypatch, launch, capsys):
    monkeypatch.setattr(cli, "load_pool", lambda: _pool())
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["codex"]) == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_the_launch_token_does_not_leak_into_later_tests():
    assert codex_cli.TOKEN_ENV not in os.environ
