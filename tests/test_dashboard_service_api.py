import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.daemon_installer as daemon_installer
import claude_unlimited.placeholder_token as placeholder_token
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
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")

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


def test_get_service_status_not_installed(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    base, _ = running_server
    status, body = _request(f"{base}/api/service")
    assert status == 200
    assert body == {"installed": False, "running": False, "pid": None}


def test_install_service_calls_installer_and_returns_status(running_server, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_installer, "install", lambda port: calls.append(port))
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 1})
    base, token = running_server
    status, body = _request(f"{base}/api/service/install", "POST", {"port": 4317}, {"X-CSRF-Token": token})
    assert status == 200
    assert calls == [4317]
    assert body["installed"] is True


def test_install_service_requires_csrf(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "install", lambda port: None)
    base, _ = running_server
    status, body = _request(f"{base}/api/service/install", "POST", {"port": 4317})
    assert status == 403


def test_install_service_failure_surfaces_error(running_server, monkeypatch):
    def boom(port):
        raise daemon_installer.DaemonInstallerError("launchctl exploded")

    monkeypatch.setattr(daemon_installer, "install", boom)
    base, token = running_server
    status, body = _request(f"{base}/api/service/install", "POST", {"port": 4317}, {"X-CSRF-Token": token})
    assert status == 500
    assert body["error"] == "install_failed"


def test_uninstall_service(running_server, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_installer, "uninstall", lambda: calls.append(True))
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    base, token = running_server
    status, body = _request(f"{base}/api/service/uninstall", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert calls == [True]
    assert body["installed"] is False


def test_get_placeholder_token(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/placeholder-token")
    assert status == 200
    assert isinstance(body["token"], str)
    assert len(body["token"]) > 10


def test_regenerate_placeholder_token_changes_value(running_server):
    base, token = running_server
    _, before = _request(f"{base}/api/placeholder-token")
    status, after = _request(f"{base}/api/placeholder-token/regenerate", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert after["token"] != before["token"]

    _, confirm = _request(f"{base}/api/placeholder-token")
    assert confirm["token"] == after["token"]


def test_regenerate_placeholder_token_requires_csrf(running_server):
    base, _ = running_server
    status, _ = _request(f"{base}/api/placeholder-token/regenerate", "POST", {})
    assert status == 403


def test_get_process_stats_are_real(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    base, _ = running_server
    status, body = _request(f"{base}/api/process")
    assert status == 200
    # The reported pid is this process's own, proving it is read from the
    # running process rather than fabricated.
    assert body["pid"] == os.getpid()
    assert body["uptime_seconds"] >= 0
    assert body["memory_mb"] > 0
    assert body["installed_as_service"] is False


def test_kill_when_installed_calls_daemon_installer_stop_not_self(running_server, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 123})
    monkeypatch.setattr(daemon_installer, "stop", lambda: calls.append(True))
    base, token = running_server
    status, body = _request(f"{base}/api/process/kill", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body == {"killed": True, "was_service": True}
    for _ in range(20):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [True]
    # Killing a service-managed instance must go through daemon_installer,
    # never self.server.shutdown(), so this server stays alive.
    status, _ = _request(f"{base}/api/process")
    assert status == 200


def test_kill_when_foreground_shuts_down_this_server(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    base, token = running_server
    status, body = _request(f"{base}/api/process/kill", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body == {"killed": True, "was_service": False}
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{base}/health", timeout=0.3)
            time.sleep(0.05)
        except (urllib.error.URLError, ConnectionError, OSError):
            # Covers a refused connection (socket already closed) and a stalled
            # one (shut down but not yet closed, so the request just hangs) —
            # both are valid evidence this server stopped serving requests.
            break
    else:
        pytest.fail("server did not shut down after a foreground kill")


def test_restart_when_installed_calls_daemon_installer_start(running_server, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 123})
    monkeypatch.setattr(daemon_installer, "start", lambda: calls.append(True))
    base, token = running_server
    status, body = _request(f"{base}/api/process/restart", "POST", {}, {"X-CSRF-Token": token})
    assert status == 200
    assert body == {"restarting": True}
    for _ in range(20):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [True]


def test_restart_when_not_installed_returns_helpful_400(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": False, "running": False, "pid": None})
    base, token = running_server
    status, body = _request(f"{base}/api/process/restart", "POST", {}, {"X-CSRF-Token": token})
    assert status == 400
    assert body["error"] == "not_installed"
    assert "install" in body["message"].lower()


def test_process_endpoints_require_csrf(running_server, monkeypatch):
    monkeypatch.setattr(daemon_installer, "status", lambda: {"installed": True, "running": True, "pid": 123})
    monkeypatch.setattr(daemon_installer, "stop", lambda: None)
    monkeypatch.setattr(daemon_installer, "start", lambda: None)
    base, _ = running_server
    status, _ = _request(f"{base}/api/process/kill", "POST", {})
    assert status == 403
    status, _ = _request(f"{base}/api/process/restart", "POST", {})
    assert status == 403
