import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo
import claude_unlimited.usage_history as usage_history


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
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status, json.loads(resp.read())


def test_profiles_api_includes_lifetime_tokens_and_cost(running_server):
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    usage_history.record(p.id, None, "claude-opus-5", {"input_tokens": 1000, "output_tokens": 500})
    usage_history.record(p.id, None, "claude-opus-5", {"input_tokens": 100, "output_tokens": 50})

    status, body = _get(f"{running_server}/api/profiles")
    assert status == 200
    profile = body["profiles"][0]
    assert profile["tokens_total"] == 1650
    assert profile["cost_usd_total"] > 0


def test_profiles_api_reports_zero_tokens_and_none_cost_for_unused_profile(running_server):
    profile_repo.create_profile(name="Y", kind="oauth", credential="tok-long-enough", account_uuid="uuid-y")

    status, body = _get(f"{running_server}/api/profiles")
    assert status == 200
    profile = body["profiles"][0]
    assert profile["tokens_total"] == 0
    assert profile["cost_usd_total"] is None
