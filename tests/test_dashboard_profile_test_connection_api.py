import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.connection_test as connection_test
import claude_unlimited.daemon as daemon
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.upstream import UpstreamResponse


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(connection_test, "secret_store", FakeSecretStore({"a": "tok-a"}))
    # test_connection()'s per-Profile throttle lives in a module-level dict, so
    # it leaks across tests in the same process unless reset here.
    monkeypatch.setattr(connection_test, "_last_test_at", {})
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True, account_uuid="uuid-a")]))

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


def test_successful_test_connection_returns_ok(running_server, monkeypatch):
    def fake_send(req, timeout=None):
        body = json.dumps({"model": connection_test.TEST_MODEL}).encode()
        return UpstreamResponse(status=200, headers={}, body_chunks=iter([body]), connection=FakeConnection())

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)
    base, token = running_server

    status, body = _request(f"{base}/api/profiles/a/test", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body["ok"] is True
    assert body["status"] == 200


def test_failed_test_connection_still_returns_200_with_ok_false(running_server, monkeypatch):
    def fake_send(req, timeout=None):
        body = json.dumps({"error": {"message": "invalid x-api-key"}}).encode()
        return UpstreamResponse(status=401, headers={}, body_chunks=iter([body]), connection=FakeConnection())

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)
    base, token = running_server

    status, body = _request(f"{base}/api/profiles/a/test", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body["ok"] is False
    assert body["message"] == "invalid x-api-key"


def test_unknown_profile_returns_502(running_server):
    base, token = running_server
    status, body = _request(f"{base}/api/profiles/nope/test", "POST", {}, {"X-CSRF-Token": token})
    assert status == 502
    assert body["ok"] is False


def test_requires_csrf(running_server):
    base, _ = running_server
    status, _ = _request(f"{base}/api/profiles/a/test", "POST", {})
    assert status == 403
