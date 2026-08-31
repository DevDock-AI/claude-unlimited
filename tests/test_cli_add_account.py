import os
import pytest

import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.cli as cli
import claude_unlimited.profiles as profile_repo


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_repo, "secret_store", FakeSecretStore())
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _fake_add_account_credentials(email="new@example.com", account_uuid="uuid-new"):
    return anthropic_oauth.ImportedCredentials(
        access_token="tok-new-long", refresh_token="ref-new", expires_at=9999, subscription_type="pro"), \
        anthropic_oauth.AccountProfile(
            account_uuid=account_uuid, email=email, display_name="New", org_uuid=None, org_name="Acme",
            has_claude_max=False, has_claude_pro=True)


def test_add_account_full_flow_uses_an_isolated_config_dir(env, monkeypatch, tmp_path, capsys):
    # The whole point of this command: it must NOT touch the default
    # Claude Code session at all — only ever operate through a fresh,
    # isolated CLAUDE_CONFIG_DIR, and remember that dir on the Profile.
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")

    captured = {}

    def fake_run(cmd, **kwargs):
        assert [os.path.basename(cmd[0]), *cmd[1:]] == ["claude", "auth", "login"]  # never "logout", never "status" — no other command needed
        captured["env"] = kwargs.get("env")
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    creds, account = _fake_add_account_credentials()
    seen = {}

    def fake_read_credentials(config_dir=None):
        seen["config_dir"] = config_dir
        return creds

    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", fake_read_credentials)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda token: account)

    rc = cli.add_account()
    assert rc == 0

    used_dir = captured["env"]["CLAUDE_CONFIG_DIR"]
    assert used_dir.startswith(str(tmp_path / "claude-accounts"))
    assert str(seen["config_dir"]) == used_dir  # same isolated dir used for login AND for reading credentials back

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1
    assert profiles[0].name == "new@example.com"
    assert profiles[0].account_uuid == "uuid-new"
    assert profiles[0].plan == "pro"
    assert profiles[0].claude_config_dir == used_dir  # remembered for reuse next time

    out = capsys.readouterr().out
    assert "Added profile" in out
    assert "will NOT log out" in out


def test_add_account_fails_cleanly_when_claude_not_on_path(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli.add_account()
    assert rc == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_add_account_fails_cleanly_when_login_itself_fails(env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(1))  # browser closed, etc.

    rc = cli.add_account()
    assert rc == 1
    assert profile_repo.list_profiles() == []


def test_add_account_run_twice_for_same_account_refreshes_not_duplicates(env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(0))

    creds, account = _fake_add_account_credentials()
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", lambda config_dir=None: creds)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda token: account)

    assert cli.add_account() == 0
    capsys.readouterr()  # discard first run's output
    assert cli.add_account() == 0  # same account, run again (e.g. its token needed a refresh)

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # not a duplicate
    out = capsys.readouterr().out
    assert "Refreshed existing profile" in out


def test_add_account_profile_lookup_failure_does_not_create_a_profile(env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(0))

    creds, _ = _fake_add_account_credentials()
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", lambda config_dir=None: creds)

    def boom(token):
        raise anthropic_oauth.ProfileLookupError("bad token")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)

    rc = cli.add_account()
    assert rc == 1
    assert profile_repo.list_profiles() == []


def test_add_account_never_calls_logout_or_status(env, monkeypatch, tmp_path):
    # Issuing `logout` or `status` would touch the default Claude Code
    # session, which is exactly the side effect this command exists to avoid.
    monkeypatch.setattr(cli, "CLAUDE_ACCOUNTS_DIR", tmp_path / "claude-accounts")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")

    def fake_run(cmd, **kwargs):
        assert "logout" not in cmd and "status" not in cmd
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    creds, account = _fake_add_account_credentials()
    monkeypatch.setattr(anthropic_oauth, "read_claude_code_credentials", lambda config_dir=None: creds)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", lambda token: account)

    assert cli.add_account() == 0
