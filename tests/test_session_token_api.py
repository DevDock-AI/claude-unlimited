import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo
import claude_unlimited.session_tokens as session_tokens


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
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", tmp_path / "session_tokens.json")

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
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_session_token_requires_profile_id(running_server):
    status, body = _get(f"{running_server}/api/session-token")
    assert status == 400
    assert body["error"] == "bad_request"


def test_session_token_404s_for_unknown_profile(running_server):
    status, body = _get(f"{running_server}/api/session-token?profile_id=does-not-exist")
    assert status == 404


def test_session_token_refuses_a_disabled_profile(running_server):
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    profile_repo.update_profile(p.id, enabled=False)
    status, body = _get(f"{running_server}/api/session-token?profile_id={p.id}")
    assert status == 400
    assert body["error"] == "disabled"


def test_session_token_mints_a_token_that_resolves_to_the_profile(running_server):
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    status, body = _get(f"{running_server}/api/session-token?profile_id={p.id}")
    assert status == 200
    assert session_tokens.resolve(body["token"]) == p.id


def test_session_token_reuses_the_same_token_on_repeated_calls(running_server):
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    status1, body1 = _get(f"{running_server}/api/session-token?profile_id={p.id}")
    status2, body2 = _get(f"{running_server}/api/session-token?profile_id={p.id}")
    assert body1["token"] == body2["token"]
