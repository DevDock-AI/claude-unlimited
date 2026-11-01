"""The `codex` CLI served from the pool (issue #7). Offline: the Codex
backend is replaced by a fake `codex_cli.send`."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited import codex_cli
from claude_unlimited.config import Pool, Profile, Settings, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.openai_credential import StoredOpenAICredential, encode
from claude_unlimited.router import ProfileState


class FakeStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def _cred(n):
    return encode(StoredOpenAICredential(access_token=f"real-{n}", refresh_token=None,
                                         account_id=f"acct-{n}", id_token=None))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "rs.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "uh.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeStore({"a": _cred("a"), "b": _cred("b"),
                                                                    "c": "sk-ant-x"}))
    return tmp_path


def _pool(enabled=True, profiles=None):
    profiles = profiles if profiles is not None else [
        Profile(id="a", name="A", kind="codex", auth_mode="chatgpt_subscription", priority=1,
                automatic=True, enabled=True),
        Profile(id="b", name="B", kind="codex", auth_mode="chatgpt_subscription", priority=2,
                automatic=True, enabled=True),
    ]
    save_pool(Pool(profiles=profiles, settings=Settings(codex_cli_serving=enabled)))


class FakeResp:
    def __init__(self, body):
        self._chunks = [body]

    def read(self, n=None):
        if n is None:
            out = b"".join(self._chunks)
            self._chunks = []
            return out
        return self._chunks.pop(0) if self._chunks else b""

    def read1(self, n):  # what the relay calls; subclasses override read()
        return self.read(n)


class FakeConn:
    def close(self):
        pass


COMPLETED = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
             b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1",'
             b'"model":"gpt-5.5","usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":40},'
             b'"output_tokens":7,"output_tokens_details":{"reasoning_tokens":3}}}}\n\n')

OK_HEADERS = {"Content-Type": "text/event-stream", "x-codex-primary-used-percent": "12",
              "x-codex-primary-window-minutes": "300", "Set-Cookie": "secret=1", "cf-ray": "abc"}


def _upstream(monkeypatch, answers):
    """answers: list of (status, headers, body), one per call. Records what was sent."""
    sent = []

    def fake_send(cred, payload, headers, timeout=None):
        sent.append({"cred": cred, "body": json.loads(payload), "headers": headers})
        status, headers_out, body = answers[min(len(sent), len(answers)) - 1]
        return status, headers_out, FakeResp(body), FakeConn()

    monkeypatch.setattr(codex_cli, "send", fake_send)
    return sent


def _request(thread="thread-1", reasoning=False):
    body = {"model": "gpt-5.5", "stream": True, "store": False, "prompt_cache_key": thread,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
    if reasoning:
        body["input"].append({"type": "reasoning", "summary": [], "encrypted_content": "gAAA-from-a"})
        body["input"].append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]})
    headers = {"Authorization": "Bearer placeholder", "session-id": thread, "thread-id": thread,
               "originator": "codex_exec", "User-Agent": "codex_exec/0.149.0 (Mac OS 26.0; arm64)",
               "x-codex-turn-state": "ts-1", "Host": "127.0.0.1:4317"}
    return headers, json.dumps(body).encode()


def _drain(result):
    return b"".join(result.body_chunks)


def test_off_by_default_and_answers_a_clear_400(env, monkeypatch):
    _pool(enabled=False)
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    result = Gateway().handle_codex_cli(*_request())
    assert result.status == 400
    assert b"turned off" in _drain(result)
    assert sent == []


def test_relays_the_body_unchanged_with_the_real_credential(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    headers, body = _request()
    result = Gateway().handle_codex_cli(headers, body)
    assert result.status == 200
    assert _drain(result) == COMPLETED                       # byte for byte
    out = sent[0]["headers"]
    assert out["Authorization"] == "Bearer real-a"            # placeholder never leaves
    assert out["ChatGPT-Account-ID"] == "acct-a"
    assert out["session-id"] == "thread-1" and out["originator"] == "codex_exec"
    assert out["version"] == "0.149.0"
    assert "Host" not in out and "host" not in out
    assert sent[0]["body"] == json.loads(body)
    # quota headers reach codex for /status; cookies never do
    assert result.headers["x-codex-primary-used-percent"] == "12"
    assert "set-cookie" not in result.headers


def test_usage_is_recorded_from_response_completed(env, monkeypatch):
    from claude_unlimited import usage_history
    _pool()
    _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    _drain(Gateway().handle_codex_cli(*_request()))
    events = usage_history.list_events()
    last = events[-1]
    assert (last.profile_id, last.model) == ("a", "gpt-5.5")
    assert (last.input_tokens, last.cache_read_input_tokens, last.output_tokens) == (60, 40, 7)


def test_an_exhausted_account_is_absorbed_and_the_request_moves(env, monkeypatch):
    """codex never retries a 429 — the daemon has to, on the next account."""
    _pool()
    sent = _upstream(monkeypatch, [
        (429, {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-after-seconds": "600"},
         b'{"error":{"type":"usage_limit_reached"}}'),
        (200, OK_HEADERS, COMPLETED)])
    gw = Gateway()
    result = gw.handle_codex_cli(*_request())
    assert result.status == 200 and result.profile_id == "b"
    assert [s["headers"]["Authorization"] for s in sent] == ["Bearer real-a", "Bearer real-b"]
    assert gw.runtime_snapshot()["a"].state != ProfileState.ELIGIBLE


def test_a_conversation_stays_on_its_account(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    gw = Gateway()
    for _ in range(3):
        _drain(gw.handle_codex_cli(*_request(reasoning=True)))
    assert {s["headers"]["Authorization"] for s in sent} == {"Bearer real-a"}
    # same account: its own ciphertext and turn token are kept
    assert any(i.get("type") == "reasoning" for i in sent[-1]["body"]["input"])
    assert sent[-1]["headers"]["x-codex-turn-state"] == "ts-1"


def test_moving_a_conversation_drops_what_only_the_old_account_can_read(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    gw = Gateway()
    _drain(gw.handle_codex_cli(*_request(reasoning=True)))          # pinned to A
    with gw._lock:
        gw._observe("a", gateway_module.QuotaExhausted(
            resets_at=datetime.now(timezone.utc) + timedelta(hours=1)), datetime.now(timezone.utc))
    _drain(gw.handle_codex_cli(*_request(reasoning=True)))          # A is out -> B
    moved = sent[-1]
    assert moved["headers"]["Authorization"] == "Bearer real-b"
    assert not any(i.get("type") == "reasoning" for i in moved["body"]["input"])
    assert [i["type"] for i in moved["body"]["input"]] == ["message", "message"]   # the conversation is intact
    assert "x-codex-turn-state" not in moved["headers"]


def test_refused_ciphertext_on_an_unknown_conversation_is_retried_without_it(env, monkeypatch):
    """A conversation begun on the user's own login, or before a restart."""
    _pool(profiles=[Profile(id="a", name="A", kind="codex", auth_mode="chatgpt_subscription",
                            automatic=True, enabled=True)])
    sent = _upstream(monkeypatch, [
        (400, {}, b'{"error":{"message":"The encrypted content could not be verified."}}'),
        (200, OK_HEADERS, COMPLETED)])
    result = Gateway().handle_codex_cli(*_request(reasoning=True))
    assert result.status == 200
    assert len(sent) == 2
    assert not any(i.get("type") == "reasoning" for i in sent[1]["body"]["input"])


def test_a_plain_bad_request_is_returned_not_rotated(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(400, {"Content-Type": "application/json"},
                                    b'{"error":{"message":"Unsupported parameter: foo"}}')])
    result = Gateway().handle_codex_cli(*_request())
    assert result.status == 400 and len(sent) == 1
    assert b"Unsupported parameter" in _drain(result)


def test_an_empty_pool_is_codex_own_usage_limit_error(env, monkeypatch):
    _pool()
    reset = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())
    _upstream(monkeypatch, [(429, {"x-codex-primary-used-percent": "100"},
                             json.dumps({"error": {"type": "usage_limit_reached", "resets_at": reset}}).encode())])
    result = Gateway().handle_codex_cli(*_request())
    assert result.status == 429
    error = json.loads(_drain(result))["error"]
    assert error["type"] == "usage_limit_reached"          # codex: "try again at HH:MM", no retry
    assert error.get("resets_at") == reset
    assert int(result.headers["retry-after"]) > 0


def test_only_chatgpt_subscription_accounts_serve_codex(env, monkeypatch):
    _pool(profiles=[
        Profile(id="c", name="Claude", kind="oauth", automatic=True, enabled=True),
        Profile(id="k", name="Key", kind="codex", auth_mode="api_key", automatic=True, enabled=True),
        Profile(id="x", name="API", kind="api", automatic=True, enabled=True),
    ])
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    result = Gateway().handle_codex_cli(*_request())
    assert result.status == 400 and sent == []
    assert b"add-codex-account" in _drain(result)


def test_a_pinned_session_never_moves(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(429, {"x-codex-primary-used-percent": "100"},
                                    b'{"error":{"type":"usage_limit_reached"}}')])
    result = Gateway().handle_codex_cli(*_request(), forced_profile_id="b")
    assert result.status == 429 and len(sent) == 1
    assert sent[0]["headers"]["Authorization"] == "Bearer real-b"


def test_pinning_a_claude_account_is_refused_clearly(env, monkeypatch):
    _pool(profiles=[Profile(id="c", name="Claude", kind="oauth", automatic=True, enabled=True)])
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    result = Gateway().handle_codex_cli(*_request(), forced_profile_id="c")
    assert result.status == 400 and sent == []
    assert b"not a ChatGPT-subscription account" in _drain(result)


def test_codex_traffic_does_not_move_claude_codes_pointer(env, monkeypatch):
    _pool()
    _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    gw = Gateway()
    gw._current_profile_id = "b"
    _drain(gw.handle_codex_cli(*_request()))
    assert gw._current_profile_id == "b"
    assert gw._codex_cli_current_id == "a"


def test_compressed_bodies_get_a_clear_400(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    headers, body = _request()
    headers["Content-Encoding"] = "zstd"
    result = Gateway().handle_codex_cli(headers, body)
    assert result.status == 400 and sent == []
    assert codex_cli.client_version("codex_exec/0.149.0 (Mac OS)") == "0.149.0"


def test_launch_overrides_point_codex_at_the_daemon_without_a_login():
    args = codex_cli.launch_overrides(4317)
    joined = " ".join(args)
    assert 'model_provider="claude-unlimited"' in joined
    assert 'base_url="http://127.0.0.1:4317/codex/v1"' in joined
    assert f'env_key="{codex_cli.TOKEN_ENV}"' in joined
    assert 'wire_api="responses"' in joined
    assert "requires_openai_auth" not in joined     # no ChatGPT login, no /models, no WebSocket


def test_the_model_list_is_relayed_from_a_pool_account(env, monkeypatch):
    _pool()
    sent = []

    def fake_send(cred, payload, headers, timeout=None, method="POST", path="/responses"):
        sent.append((method, path, headers))
        return 200, {"Content-Type": "application/json", "X-Models-Etag": "e1", "Set-Cookie": "x"}, \
            FakeResp(b'{"models":[]}'), FakeConn()

    monkeypatch.setattr(codex_cli, "send", fake_send)
    result = Gateway().handle_codex_cli_models({"User-Agent": "codex_exec/0.149.0"}, "client_version=0.149.0")
    assert result.status == 200 and _drain(result) == b'{"models":[]}'
    method, path, headers = sent[0]
    assert (method, path) == ("GET", "/models?client_version=0.149.0")
    assert headers["Authorization"] == "Bearer real-a" and "Content-Length" not in headers
    assert result.headers["x-models-etag"] == "e1" and "set-cookie" not in result.headers


def test_no_model_list_while_off_or_on_failure_codex_keeps_its_own(env, monkeypatch):
    _pool(enabled=False)
    assert Gateway().handle_codex_cli_models({}, "").status == 404
    _pool()
    monkeypatch.setattr(codex_cli, "send", lambda *a, **k: (_ for _ in ()).throw(
        gateway_module.openai_bridge.OpenAIBridgeError("offline")))
    assert Gateway().handle_codex_cli_models({}, "").status == 404


def test_a_pinned_session_that_cannot_reach_openai_is_a_400_not_a_5xx(env, monkeypatch):
    # codex retries any 5xx ~30 times in 24s; a pinned session has nowhere
    # else to go, so the failure is said once.
    _pool()

    def unreachable(*a, **kw):
        raise gateway_module.openai_bridge.OpenAIBridgeError("offline")

    monkeypatch.setattr(codex_cli, "send", unreachable)
    result = Gateway().handle_codex_cli(*_request(), forced_profile_id="b")
    assert result.status == 400
    assert b"Could not reach OpenAI" in _drain(result)


def test_relay_passes_each_sse_event_on_as_it_arrives():
    # Over a real chunked response: read(8192) would wait for 8 KB, so the
    # first small event would only arrive when the stream ended. The server
    # holds the stream open until the relay has yielded that first event.
    import http.client
    import http.server
    import threading

    first_seen = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event, wait in ((b"data: one\n\n", True), (b"data: two\n\n", False)):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
                if wait:
                    first_seen.wait(5)
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/")
        stream = codex_cli.relay(conn.getresponse(), conn)
        assert next(stream) == b"data: one\n\n"
        first_seen.set()
        assert b"".join(stream) == b"data: two\n\n"
    finally:
        first_seen.set()
        server.server_close()


class TruncatedResp(FakeResp):
    def read(self, n=None):
        import http.client
        raise http.client.IncompleteRead(b'{"err')


class ClosingConn(FakeConn):
    closed = False

    def close(self):
        self.closed = True


def _truncated_error_upstream(monkeypatch, then_ok=True):
    conns = []

    def fake_send(cred, payload, headers, timeout=None):
        conns.append(ClosingConn())
        if len(conns) == 1 or not then_ok:
            return 503, {}, TruncatedResp(b""), conns[-1]
        return 200, OK_HEADERS, FakeResp(COMPLETED), conns[-1]

    monkeypatch.setattr(codex_cli, "send", fake_send)
    return conns


def test_an_error_body_cut_short_frees_the_account_and_rotates(env, monkeypatch):
    _pool()
    conns = _truncated_error_upstream(monkeypatch)
    gw = Gateway()
    result = gw.handle_codex_cli(*_request())
    assert result.status == 200 and result.profile_id == "b"
    assert conns[0].closed and "a" not in gw._in_flight


def test_a_pinned_error_body_cut_short_is_a_400_not_a_5xx(env, monkeypatch):
    _pool()
    conns = _truncated_error_upstream(monkeypatch, then_ok=False)
    gw = Gateway()
    result = gw.handle_codex_cli(*_request(), forced_profile_id="b")
    assert result.status == 400 and len(conns) == 1
    assert conns[0].closed and "b" not in gw._in_flight


def test_a_pinned_account_whose_credential_cannot_be_read_says_so(env, monkeypatch):
    _pool()

    class LockedStore:
        def get_token(self, profile_id):
            raise RuntimeError("keychain locked")

    monkeypatch.setattr(gateway_module, "secret_store", LockedStore())
    result = Gateway().handle_codex_cli(*_request(), forced_profile_id="b")
    assert result.status == 400
    assert b"stored credential that could not be read" in _drain(result)


def test_a_chatgpt_account_needing_reauth_points_at_add_codex_account(env, monkeypatch):
    _pool()
    result = Gateway()._codex_cli_unavailable(
        gateway_module.load_pool(), None,
        "forced_profile_needs_reauth", "b")
    text = _drain(result)
    assert b"add-codex-account" in text and b"claude-unlimited reauth" not in text


def test_a_body_over_the_codex_limit_is_an_openai_413_not_sent(env, monkeypatch):
    from claude_unlimited.proxy import CODEX_MAX_REQUEST_BYTES
    _pool()
    sent = _upstream(monkeypatch, [(200, OK_HEADERS, COMPLETED)])
    headers, _ = _request()
    body = json.dumps({"model": "gpt-5.5", "input": "x" * (CODEX_MAX_REQUEST_BYTES + 1)}).encode()
    result = Gateway().handle_codex_cli(headers, body)
    assert result.status == 413
    assert json.loads(_drain(result))["error"]["type"] == "request_too_large"
    assert sent == []


def test_a_402_account_is_cooled_for_an_hour_and_the_request_moves(env, monkeypatch):
    _pool()
    sent = _upstream(monkeypatch, [(402, {}, b'{"error":{"type":"payment_required"}}'),
                                   (200, OK_HEADERS, COMPLETED)])
    gw = Gateway()
    result = gw.handle_codex_cli(*_request())
    assert result.status == 200 and result.profile_id == "b"
    assert len(sent) == 2
    rt = gw.runtime_snapshot()["a"]
    assert rt.budget_unavailable and rt.state == ProfileState.COOLDOWN
    assert rt.cooldown_until - datetime.now(timezone.utc) > timedelta(minutes=55)


def test_every_account_402_is_never_a_5xx(env, monkeypatch):
    _pool()
    _upstream(monkeypatch, [(402, {}, b'{"error":{"type":"payment_required"}}')])
    result = Gateway().handle_codex_cli(*_request())
    assert result.status < 500


def test_openai_classify_maps_402_to_budget_unavailable():
    from claude_unlimited import openai_observation
    from claude_unlimited.observation import BudgetUnavailable
    assert isinstance(openai_observation.classify(402, {}, datetime.now(timezone.utc)), BudgetUnavailable)
