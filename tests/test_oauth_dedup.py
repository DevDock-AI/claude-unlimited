import json
import threading
import urllib.request

import pytest

import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.daemon as daemon
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
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    return store


def test_upsert_oauth_profile_creates_when_no_existing_account(env):
    profile, reused = profile_repo.upsert_oauth_profile(
        name="X", account_uuid="uuid-1", credential="tok-original-long", plan="pro",
        refresh_token="ref-1", expires_at=1000)
    assert reused is False
    assert profile.name == "X"
    assert profile.plan == "pro"

    import claude_unlimited.oauth_credential as oauth_credential
    stored = oauth_credential.decode(env.get_token(profile.id))
    assert stored.access_token == "tok-original-long"
    assert stored.refresh_token == "ref-1"


def test_upsert_oauth_profile_refreshes_credential_and_plan_when_account_exists(env):
    first, _ = profile_repo.upsert_oauth_profile(
        name="X", account_uuid="uuid-1", credential="tok-1-long", plan="pro")

    second, reused = profile_repo.upsert_oauth_profile(
        name="X (renamed on reimport)", account_uuid="uuid-1", credential="tok-2-long", plan="max",
        refresh_token="ref-2", expires_at=2000)

    assert reused is True
    assert second.id == first.id  # same Profile, not a duplicate
    assert second.plan == "max"  # plan kept current on refresh, not just on first add

    import claude_unlimited.oauth_credential as oauth_credential
    stored = oauth_credential.decode(env.get_token(first.id))
    assert stored.access_token == "tok-2-long"
    assert stored.refresh_token == "ref-2"

    profiles = profile_repo.list_profiles()
    assert len(profiles) == 1  # never duplicated


def test_upsert_oauth_profile_keeps_existing_plan_when_none_given(env):
    first, _ = profile_repo.upsert_oauth_profile(name="X", account_uuid="uuid-1", credential="tok-1-long", plan="pro")
    second, reused = profile_repo.upsert_oauth_profile(name="X", account_uuid="uuid-1", credential="tok-2-long", plan=None)
    assert reused is True
    assert second.plan == "pro"  # untouched when the caller has nothing new to say


def test_find_by_account_uuid(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-original-long", account_uuid="uuid-1")
    found = profile_repo.find_by_account_uuid("uuid-1")
    assert found is not None
    assert found.name == "X"
    assert profile_repo.find_by_account_uuid("uuid-missing") is None


def test_update_credential_replaces_keychain_token_only(env):
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-original-long", account_uuid="uuid-1")
    profile_repo.update_credential(p.id, "tok-refreshed-long")
    assert env.get_token(p.id) == "tok-refreshed-long"
    # metadata (name, priority, etc.) is untouched
    reloaded = profile_repo.find_by_account_uuid("uuid-1")
    assert reloaded.id == p.id
    assert reloaded.name == "X"


def test_create_profile_and_update_credential_store_refresh_token_and_expiry(env):
    import claude_unlimited.oauth_credential as oauth_credential

    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-1-long", account_uuid="uuid-1",
                                     refresh_token="ref-1", expires_at=1000)
    stored = oauth_credential.decode(env.get_token(p.id))
    assert stored.access_token == "tok-1-long"
    assert stored.refresh_token == "ref-1"
    assert stored.expires_at == 1000

    profile_repo.update_credential(p.id, "tok-2-long", refresh_token="ref-2", expires_at=2000)
    stored_again = oauth_credential.decode(env.get_token(p.id))
    assert stored_again.access_token == "tok-2-long"
    assert stored_again.refresh_token == "ref-2"
    assert stored_again.expires_at == 2000


def test_update_credential_without_refresh_token_stores_plain_string(env):
    # A manually pasted token has no refresh_token, so it stays a plain string
    # rather than being wrapped in JSON for no reason.
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-original-long", account_uuid="uuid-1")
    profile_repo.update_credential(p.id, "tok-refreshed-long")
    assert env.get_token(p.id) == "tok-refreshed-long"


def test_update_credential_stamps_credential_updated_at(env):
    # This stamp is how a running Gateway notices a re-auth and clears a stuck
    # AUTH_INVALID state (see gateway.py's _sync_snapshot). Without it,
    # re-authenticating a registered Profile leaves no trace in config.json.
    p = profile_repo.create_profile(name="X", kind="oauth", credential="tok-original-long", account_uuid="uuid-1")
    assert p.credential_updated_at is None

    profile_repo.update_credential(p.id, "tok-refreshed-long")
    reloaded = profile_repo.find_by_account_uuid("uuid-1")
    assert reloaded.credential_updated_at is not None

    first_stamp = reloaded.credential_updated_at
    profile_repo.update_credential(p.id, "tok-refreshed-again-long")
    reloaded_again = profile_repo.find_by_account_uuid("uuid-1")
    assert reloaded_again.credential_updated_at != first_stamp  # each refresh bumps it


def test_import_claude_code_second_time_updates_instead_of_duplicating(env, monkeypatch, tmp_path):
    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def fake_read():
        return anthropic_oauth.ImportedCredentials(access_token="tok-1-long", refresh_token=None,
                                                     expires_at=None, subscription_type="max")

    def fake_fetch(token, timeout=15.0):
        return anthropic_oauth.AccountProfile(account_uuid="same-account-uuid", email="dev@example.com",
                                                display_name="Dev", org_uuid=None, org_name=None,
                                                has_claude_max=True, has_claude_pro=False)

    monkeypatch.setattr(daemon.anthropic_oauth, "read_claude_code_credentials", fake_read)
    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fake_fetch)

    try:
        token = daemon._CSRF_TOKEN

        req1 = urllib.request.Request(f"{base}/api/import-claude-code", method="POST", data=b"{}",
                                       headers={"X-CSRF-Token": token})
        with urllib.request.urlopen(req1, timeout=2) as resp1:
            body1 = json.loads(resp1.read())
            assert resp1.status == 201
            assert body1["reused_existing"] is False

        req2 = urllib.request.Request(f"{base}/api/import-claude-code", method="POST", data=b"{}",
                                       headers={"X-CSRF-Token": token})
        with urllib.request.urlopen(req2, timeout=2) as resp2:
            body2 = json.loads(resp2.read())
            assert resp2.status == 200
            assert body2["reused_existing"] is True
            assert body2["profile"]["id"] == body1["profile"]["id"]

        with urllib.request.urlopen(f"{base}/api/profiles", timeout=2) as resp3:
            profiles = json.loads(resp3.read())["profiles"]
        assert len(profiles) == 1  # no duplicate
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()
