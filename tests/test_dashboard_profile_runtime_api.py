import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.gateway as gateway_module
import claude_unlimited.profiles as profile_repo
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.upstream import UpstreamResponse


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


class FakeConnection:
    def close(self):
        pass


def fake_response(status, headers=None, body=b"ok"):
    def chunks():
        if body:
            yield body

    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(gateway_module, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")

    # make_server() rebuilds daemon._gateway fresh on every call, so it must
    # run AFTER the monkeypatches above to pick up the isolated runtime_state
    # path. The explicit resets below then guard against make_server() ever
    # handing back a shared instance instead.
    server = daemon.make_server(host="127.0.0.1", port=0)
    daemon._gateway._runtime = {}
    daemon._gateway._current_profile_id = None
    daemon._gateway._warned_approaching = set()
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN, store
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


def test_freshly_created_profile_has_no_runtime_yet(running_server):
    base, token, _ = running_server
    _request(f"{base}/api/profiles", "POST",
             {"name": "A", "kind": "oauth", "credential": "tok-long-enough", "account_uuid": "u1"},
             headers={"X-CSRF-Token": token})
    status, body = _request(f"{base}/api/profiles")
    assert status == 200
    p = body["profiles"][0]
    assert p["state"] == "eligible"
    assert p["status_word"] == "healthy"
    assert p["usage_5h_percent"] is None
    assert p["usage_7d_percent"] is None


def test_disabled_profile_reports_disabled_state_before_any_sync(running_server):
    base, token, _ = running_server
    status, body = _request(f"{base}/api/profiles", "POST",
                             {"name": "A", "kind": "oauth", "credential": "tok-long-enough", "account_uuid": "u1"},
                             headers={"X-CSRF-Token": token})
    profile_id = body["profile"]["id"]
    _request(f"{base}/api/profiles/{profile_id}", "PATCH", {"enabled": False}, headers={"X-CSRF-Token": token})

    status, body = _request(f"{base}/api/profiles")
    assert body["profiles"][0]["state"] == "disabled"
    assert body["profiles"][0]["status_word"] == "disabled"


def test_real_request_populates_5h_and_7d_usage_in_api(running_server, monkeypatch):
    base, token, store = running_server
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
    ]))
    store.set_token("a", "tok-a")
    monkeypatch.setattr(daemon._gateway, "_transport", lambda req: fake_response(
        200, {"anthropic-ratelimit-unified-5h-utilization": "0.42", "anthropic-ratelimit-unified-5h-reset": "1787191800",
              "anthropic-ratelimit-unified-7d-utilization": "0.19", "anthropic-ratelimit-unified-7d-reset": "1787666400"}))

    result = daemon._gateway.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200

    status, body = _request(f"{base}/api/profiles")
    p = next(p for p in body["profiles"] if p["id"] == "a")
    assert p["state"] == "eligible"
    assert p["status_word"] == "healthy"
    assert p["usage_5h_percent"] == 42.0
    assert p["usage_7d_percent"] == 19.0
    assert p["usage_5h_resets_at"] is not None
    assert p["usage_7d_resets_at"] is not None


def test_status_reports_real_version_and_uptime(running_server):
    base, _, _ = running_server
    time.sleep(0.05)
    status, body = _request(f"{base}/api/status")
    assert status == 200
    assert body["version"] == daemon.__version__
    assert body["uptime_seconds"] >= 0
