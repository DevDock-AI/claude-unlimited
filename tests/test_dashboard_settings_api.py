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
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN, tmp_path
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


def test_get_settings_defaults(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/settings")
    assert status == 200
    assert body["settings"]["update_mode"] == "auto_download"


def test_patch_settings_updates_and_persists(running_server):
    base, token, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"update_mode": "manual"},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["settings"]["update_mode"] == "manual"

    status, body = _request(f"{base}/api/settings")
    assert body["settings"]["update_mode"] == "manual"


def test_patch_settings_rejects_invalid_update_mode(running_server):
    base, token, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"update_mode": "yolo"},
                             headers={"X-CSRF-Token": token})
    assert status == 400


def test_patch_settings_requires_csrf(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"update_mode": "manual"})
    assert status == 403


def test_get_locales_lists_available_and_current(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/locales")
    assert status == 200
    assert "en" in body["available"]
    assert "es" in body["available"]
    assert body["current"] == "en"
    assert body["names"]["es"] == "Español"


def test_get_specific_locale_strings(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/locales/es")
    assert status == 200
    assert body["strings"]["nav.overview"] == "Resumen"


def test_get_unknown_locale_404s(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/locales/zz")
    assert status == 404
    assert body["error"] == "unknown_locale"


def test_get_locale_rejects_path_traversal(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/locales/..%2F..%2F..%2Fetc%2Fpasswd")
    assert status == 404


def test_patch_settings_language_updates_and_persists(running_server):
    base, token, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"language": "es"},
                             headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["settings"]["language"] == "es"

    status, body = _request(f"{base}/api/locales")
    assert body["current"] == "es"


def test_patch_settings_rejects_invalid_language(running_server):
    base, token, _ = running_server
    status, body = _request(f"{base}/api/settings", "PATCH", {"language": "not-a-real-locale"},
                             headers={"X-CSRF-Token": token})
    assert status == 400


def test_reset_removes_all_profiles_and_credentials(running_server):
    base, token, _ = running_server
    _request(f"{base}/api/profiles", "POST",
             {"name": "A", "kind": "oauth", "credential": "long-enough-token", "account_uuid": "u1"},
             headers={"X-CSRF-Token": token})
    _request(f"{base}/api/profiles", "POST",
             {"name": "B", "kind": "oauth", "credential": "long-enough-token", "account_uuid": "u2"},
             headers={"X-CSRF-Token": token})

    status, body = _request(f"{base}/api/reset", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 200
    assert body["removed"] == 2

    status, body = _request(f"{base}/api/profiles")
    assert body["profiles"] == []


def test_reset_requires_csrf(running_server):
    base, _, _ = running_server
    status, body = _request(f"{base}/api/reset", "POST", {})
    assert status == 403
