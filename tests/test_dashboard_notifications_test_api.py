import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.notifications as notifications


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")

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


def test_test_notification_calls_send_macos_notification(running_server, monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda title, message: calls.append((title, message)))
    base, token = running_server
    status, body = _request(f"{base}/api/notifications/test", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body == {"sent": True}
    assert len(calls) == 1
    assert "test" in calls[0][1].lower()


def test_test_notification_requires_csrf(running_server):
    base, _ = running_server
    status, _ = _request(f"{base}/api/notifications/test", "POST", {})
    assert status == 403


def test_test_notification_ignores_settings_gate(running_server, monkeypatch):
    # Explicit "test" action must fire regardless of notifications_enabled —
    # that's the whole point (verify the OS mechanism works before trusting it).
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda title, message: calls.append(True))
    base, token = running_server
    _request(f"{base}/api/settings", "PATCH", {"notifications_enabled": False}, {"X-CSRF-Token": token})
    status, body = _request(f"{base}/api/notifications/test", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert len(calls) == 1
