import json
import threading
import urllib.error
import urllib.request

import pytest

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
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_repo, "secret_store", FakeSecretStore())
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _request(url, method="GET", body=None, headers=None):
    headers = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_get_profiles_empty_list(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/profiles")
    assert status == 200
    assert body["profiles"] == []


def test_post_profile_without_csrf_token_is_rejected(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/profiles", "POST", {"name": "X", "kind": "oauth", "credential": "sk-ant-12345678"})
    assert status == 403
    assert body["error"] == "csrf"


def test_post_profile_with_wrong_csrf_token_is_rejected(running_server):
    base, _ = running_server
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "X", "kind": "oauth", "credential": "sk-ant-12345678"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert status == 403


def test_full_crud_roundtrip_with_valid_csrf(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Personal Max", "kind": "oauth", "credential": "sk-ant-12345678", "account_uuid": "acct-test"},
        headers=headers,
    )
    assert status == 201
    profile_id = body["profile"]["id"]
    assert "credential" not in body["profile"]

    status, body = _request(f"{base}/api/profiles")
    assert len(body["profiles"]) == 1

    status, body = _request(f"{base}/api/profiles/{profile_id}", "PATCH", {"priority": 2}, headers=headers)
    assert status == 200
    assert body["profile"]["priority"] == 2

    status, body = _request(f"{base}/api/profiles/{profile_id}", "DELETE", headers=headers)
    assert status == 200

    status, body = _request(f"{base}/api/profiles")
    assert body["profiles"] == []


def test_update_credential_rotates_the_stored_secret(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST",
                             {"credential": "sk-ant-rotated-key"}, headers=headers)
    assert status == 200

    # The stored shape is oauth_credential-encoded, not the raw string —
    # decoding it is what actually proves the rotation happened.
    import claude_unlimited.oauth_credential as oauth_credential
    decoded = oauth_credential.decode(profile_repo.secret_store.get_token(profile_id))
    assert decoded.access_token == "sk-ant-rotated-key"


def test_update_credential_requires_csrf(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST", {"credential": "sk-ant-new-key"})
    assert status == 403


def test_update_credential_404s_for_unknown_profile(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(f"{base}/api/profiles/does-not-exist/credential", "POST",
                             {"credential": "sk-ant-new-key"}, headers=headers)
    assert status == 404


def test_update_credential_rejects_too_short_a_value(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST", {"credential": "short"}, headers=headers)
    assert status == 400


def test_invalid_host_header_is_rejected(running_server):
    base, token = running_server
    status, body = _request(f"{base}/api/profiles", headers={"Host": "evil.example.com"})
    assert status == 400
    assert body["error"] == "invalid_host"


def test_no_cors_header_ever_sent(running_server):
    base, _ = running_server
    req = urllib.request.Request(f"{base}/api/status")
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.headers.get("Access-Control-Allow-Origin") is None


def test_responses_never_cached(running_server):
    base, _ = running_server
    req = urllib.request.Request(f"{base}/api/status")
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.headers.get("Cache-Control") == "no-store"
