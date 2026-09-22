import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
from claude_unlimited import placeholder_token
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


def fake_response(status, headers=None, body=b'{"ok":true}'):
    def chunks():
        yield body

    return UpstreamResponse(status=status, headers=headers or {"content-type": "application/json"},
                             body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def running_proxy_server(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(placeholder_token, "APP_DIR", tmp_path)
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr("claude_unlimited.gateway.secret_store", FakeSecretStore({"a": "real-tok"}))
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))

    # make_server() rebuilds daemon._gateway fresh on every call, so it must
    # run BEFORE the overrides below or it would replace the instance they
    # configure.
    server = daemon.make_server(host="127.0.0.1", port=0)
    daemon._gateway._transport = lambda req: fake_response(200)
    daemon._gateway._runtime = {}
    daemon._gateway._current_profile_id = None
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def test_proxy_request_without_placeholder_token_is_401(running_proxy_server):
    req = urllib.request.Request(f"{running_proxy_server}/v1/messages", data=b"{}", method="POST")
    try:
        urllib.request.urlopen(req, timeout=2)
        assert False, "expected 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_proxy_request_with_correct_placeholder_token_succeeds(running_proxy_server):
    token = placeholder_token.get_or_create()
    req = urllib.request.Request(
        f"{running_proxy_server}/v1/messages", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"ok": True}


def test_proxy_request_with_wrong_placeholder_token_is_401(running_proxy_server):
    req = urllib.request.Request(
        f"{running_proxy_server}/v1/messages", data=b"{}", method="POST",
        headers={"Authorization": "Bearer wrong-token"},
    )
    try:
        urllib.request.urlopen(req, timeout=2)
        assert False, "expected 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_the_capacity_guards_400_reaches_the_client_in_anthropics_exact_shape(monkeypatch, tmp_path):
    """docs/adr/0009: Claude Code's reactive compaction keys on this envelope
    and this wording — invalid_request_error, "prompt is too long: N tokens >
    M maximum", no [claude-unlimited] prefix, and never overloaded_error."""
    import threading
    from claude_unlimited import daemon

    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(placeholder_token, "APP_DIR", tmp_path)
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr("claude_unlimited.gateway.secret_store", FakeSecretStore({"c": "tok"}))
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    save_pool(Pool(profiles=[Profile(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription",
                                     automatic=True, enabled=True)]))
    monkeypatch.setattr("claude_unlimited.gateway.openai_bridge.run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reach OpenAI")))

    server = daemon.make_server(host="127.0.0.1", port=0)
    daemon._gateway._runtime = {}
    daemon._gateway._current_profile_id = None
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        body = json.dumps({"model": "claude-opus-5", "stream": True,
                           "messages": [{"role": "user", "content": "x" * 1_200_000}]}).encode()
        token = placeholder_token.get_or_create()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert e.headers.get("Retry-After") is None
            payload = json.loads(e.read())
        from claude_unlimited import gpt_windows
        estimate = gpt_windows.estimate_input_tokens(body, json.loads(body))
        assert payload == {"type": "error",
                           "error": {"type": "invalid_request_error",
                                     "message": f"prompt is too long: {estimate} tokens > 226400 maximum"}}
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()
