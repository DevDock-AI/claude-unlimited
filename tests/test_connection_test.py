import json

import pytest

from claude_unlimited import connection_test
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
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    # test_connection()'s per-Profile throttle lives in a module-level dict, so
    # it leaks across tests in the same process unless reset here.
    monkeypatch.setattr(connection_test, "_last_test_at", {})
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True, account_uuid="uuid-a"),
        Profile(id="b", name="B", kind="api", automatic=True, enabled=True, base_url="https://api.anthropic.com"),
    ]))
    return tmp_path


def test_successful_ping_reports_ok_and_timing(pool_env, monkeypatch):
    monkeypatch.setattr(connection_test, "secret_store", FakeSecretStore({"a": "tok-a"}))

    captured = {}

    def fake_send(req, timeout=None):
        captured["req"] = req
        body = json.dumps({"model": connection_test.TEST_MODEL}).encode()
        return UpstreamResponse(status=200, headers={}, body_chunks=iter([body]), connection=FakeConnection())

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)

    result = connection_test.test_connection("a")

    assert result["ok"] is True
    assert result["status"] == 200
    assert isinstance(result["elapsed_ms"], int)
    assert result["model"] == connection_test.TEST_MODEL

    # The stored credential was used, and the account_uuid was embedded the way
    # Claude Code traffic embeds it (see proxy.py's docstring).
    req = captured["req"]
    assert req.headers["Authorization"] == "Bearer tok-a"
    sent_body = json.loads(req.body)
    assert sent_body["max_tokens"] == 1
    user_id = json.loads(sent_body["metadata"]["user_id"])
    assert user_id["account_uuid"] == "uuid-a"


def test_api_key_profile_never_gets_oauth_metadata(pool_env, monkeypatch):
    monkeypatch.setattr(connection_test, "secret_store", FakeSecretStore({"b": "sk-b"}))

    captured = {}

    def fake_send(req, timeout=None):
        captured["req"] = req
        return UpstreamResponse(status=200, headers={}, body_chunks=iter([b"{}"]), connection=FakeConnection())

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)

    connection_test.test_connection("b")

    req = captured["req"]
    assert req.headers["x-api-key"] == "sk-b"
    assert "Authorization" not in req.headers
    sent_body = json.loads(req.body)
    assert "metadata" not in sent_body


def test_upstream_error_response_is_reported_not_raised(pool_env, monkeypatch):
    monkeypatch.setattr(connection_test, "secret_store", FakeSecretStore({"a": "tok-a"}))

    def fake_send(req, timeout=None):
        body = json.dumps({"error": {"message": "invalid x-api-key"}}).encode()
        return UpstreamResponse(status=401, headers={}, body_chunks=iter([body]), connection=FakeConnection())

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)

    result = connection_test.test_connection("a")

    assert result["ok"] is False
    assert result["status"] == 401
    assert result["message"] == "invalid x-api-key"


def test_unknown_profile_raises(pool_env):
    with pytest.raises(connection_test.ConnectionTestError):
        connection_test.test_connection("does-not-exist")


def test_network_failure_raises_connection_test_error(pool_env, monkeypatch):
    monkeypatch.setattr(connection_test, "secret_store", FakeSecretStore({"a": "tok-a"}))

    def fake_send(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(connection_test.upstream, "send", fake_send)

    with pytest.raises(connection_test.ConnectionTestError):
        connection_test.test_connection("a")
