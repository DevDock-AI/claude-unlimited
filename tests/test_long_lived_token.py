"""A Claude subscription added from a `claude setup-token` token: inference
scope only (no profile lookup), no refresh token, valid about a year.

Fully offline: every provider call is replaced."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import claude_unlimited.anthropic_oauth as oauth_module
import claude_unlimited.daemon as daemon
import claude_unlimited.gateway as gateway_module
import claude_unlimited.oauth_credential as oauth_credential
import claude_unlimited.oauth_login as oauth_login
import claude_unlimited.profiles as profile_repo
import claude_unlimited.usage_probe as usage_probe
from claude_unlimited.config import Pool, Profile, load_pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.router import ProfileState

UUID = "1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f"
TOKEN = "sk-ant-oat01-test-placeholder-not-real"


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
def store(monkeypatch, tmp_path):
    s = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", s)
    monkeypatch.setattr(gateway_module, "secret_store", s)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(daemon, "_prime_profile_async", lambda pid: None)
    return s


@pytest.fixture
def server(store):
    srv = daemon.make_server(host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", daemon._CSRF_TOKEN
    finally:
        srv.shutdown()
        t.join(timeout=2)
        srv.server_close()


def _post(base, csrf, body):
    req = urllib.request.Request(f"{base}/api/profiles", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-CSRF-Token": csrf})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_supplied_uuid_skips_lookup_and_stores_a_one_year_non_refreshable_credential(server, store, monkeypatch):
    base, csrf = server

    def fail_fetch(*a, **kw):
        raise AssertionError("lookup must be skipped when account_uuid is supplied")

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", fail_fetch)
    before = int(time.time() * 1000)
    status, body = _post(base, csrf, {"name": "Headless", "kind": "oauth", "long_lived": True,
                                      "credential": TOKEN, "account_uuid": UUID.upper(), "plan": "max"})
    assert status == 201, body
    p = body["profile"]
    assert p["account_uuid"] == UUID and p["plan"] == "max"
    year = daemon.LONG_LIVED_TOKEN_TTL_MS
    assert before + year <= p["token_expires_at"] <= before + year + 60_000
    cred = oauth_credential.decode(store.tokens[p["id"]])
    assert cred.access_token == TOKEN and cred.refresh_token is None
    assert cred.expires_at == p["token_expires_at"]
    assert load_pool().get(p["id"]).token_expires_at == p["token_expires_at"]


def test_auto_plan_is_left_unset(server, monkeypatch):
    base, csrf = server
    status, body = _post(base, csrf, {"name": "H", "kind": "oauth", "long_lived": True,
                                      "credential": TOKEN, "account_uuid": UUID, "plan": "auto"})
    assert status == 201, body
    assert body["profile"]["plan"] is None


def test_malformed_uuid_is_rejected(server):
    base, csrf = server
    status, body = _post(base, csrf, {"name": "H", "kind": "oauth", "long_lived": True,
                                      "credential": TOKEN, "account_uuid": "not-a-uuid"})
    assert status == 400 and body["error"] == "validation"


def test_missing_uuid_with_scope_403_asks_for_the_uuid(server, monkeypatch):
    base, csrf = server

    def scope_fetch(*a, **kw):
        raise oauth_module.ProfileLookupError("scope", scope_limited=True)

    monkeypatch.setattr(daemon.anthropic_oauth, "fetch_account_profile", scope_fetch)
    status, body = _post(base, csrf, {"name": "H", "kind": "oauth", "long_lived": True, "credential": TOKEN})
    assert status == 400
    assert body["error"] == "account_uuid_required"
    assert "accountUuid" in body["message"]


def test_scope_403_from_the_profile_endpoint_is_marked_scope_limited(monkeypatch):
    class Err(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 403, "Forbidden", {}, None)

        def read(self):
            return b'{"error":{"message":"OAuth token does not meet scope requirement any_of(user:profile)"}}'

    def raise_403(*a, **kw):
        raise Err()

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", raise_403)
    with pytest.raises(oauth_module.ProfileLookupError) as info:
        oauth_module.fetch_account_profile(TOKEN)
    assert info.value.scope_limited is True
    assert "Account UUID" in str(info.value)


def test_full_login_clears_the_long_lived_marker(store):
    p = profile_repo.create_profile(name="H", kind="oauth", credential=TOKEN, account_uuid=UUID,
                                    expires_at=1, token_expires_at=1)
    updated, reused = profile_repo.upsert_oauth_profile(name="H", account_uuid=UUID, credential="new-access-token",
                                                         refresh_token="rt", expires_at=2)
    assert reused and updated.id == p.id and updated.token_expires_at is None


def _long_lived_profile(store, expires_at):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", account_uuid=UUID, priority=1,
                                     automatic=True, enabled=True, token_expires_at=expires_at)]))
    store.tokens["a"] = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token=TOKEN, refresh_token=None, expires_at=expires_at))


def test_no_refresh_is_attempted_without_a_refresh_token(store, monkeypatch):
    # Already past its expiry, so only the missing refresh token stops a refresh.
    _long_lived_profile(store, expires_at=1)

    def no_refresh(*a, **kw):
        raise AssertionError("must never refresh a credential with no refresh_token")

    monkeypatch.setattr(oauth_login, "refresh_access_token", no_refresh)
    gw = Gateway(transport=lambda req: None)
    profile = load_pool().get("a")
    assert gw._maybe_refresh_credential(profile, store.tokens["a"]) == TOKEN
    gw.runtime_snapshot()  # runs the background credential check too


def test_usage_probe_scope_403_does_not_flag_reauth(store, monkeypatch, tmp_path):
    _long_lived_profile(store, expires_at=int(time.time() * 1000) + daemon.LONG_LIVED_TOKEN_TTL_MS)
    gw = Gateway(transport=lambda req: None)
    monkeypatch.setattr(daemon, "_gateway", gw)
    monkeypatch.setattr(daemon, "_usage_probe", usage_probe.Scheduler(state_file=tmp_path / "ups.json"))
    seen = []

    def fake_fetch(token):
        seen.append(token)
        return usage_probe.ProbeResult(status=403)

    monkeypatch.setattr(usage_probe, "fetch_anthropic_usage", fake_fetch)
    daemon._probe_usage(load_pool().get("a"))
    assert seen == [TOKEN]  # the bare token, not the stored JSON blob
    rt = gw.runtime_snapshot().get("a")
    assert rt is None or rt.state != ProfileState.AUTH_INVALID
