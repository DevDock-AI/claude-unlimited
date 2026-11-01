import json

from claude_unlimited.config import Profile
from claude_unlimited.proxy import (
    ANTHROPIC_DEFAULT_BASE_URL,
    CODEX_MAX_REQUEST_BYTES,
    MESSAGES_MAX_REQUEST_BYTES,
    RequestTooLarge,
    check_request_size,
    max_request_bytes,
    build_upstream_request,
    filter_response_headers,
    request_model,
    resolve_base_url,
    rewrite_model,
)


def oauth_profile(**kw):
    return Profile(id="a", name="Personal Max", kind="oauth", account_uuid="acct-123", **kw)


def api_profile(**kw):
    return Profile(id="b", name="API", kind="api", **kw)


def test_oauth_profile_always_resolves_to_anthropic_even_with_base_url_set():
    p = oauth_profile(base_url="https://should-be-ignored.example")
    assert resolve_base_url(p) == ANTHROPIC_DEFAULT_BASE_URL


def test_api_profile_uses_its_own_base_url():
    p = api_profile(base_url="https://gateway.example/v1")
    assert resolve_base_url(p) == "https://gateway.example/v1"


def test_api_profile_with_no_base_url_defaults_to_anthropic():
    p = api_profile(base_url=None)
    assert resolve_base_url(p) == ANTHROPIC_DEFAULT_BASE_URL


def test_strips_inbound_credential_and_hop_by_hop_headers():
    p = oauth_profile()
    req = build_upstream_request(
        p, "real-token-xyz", "POST", "/v1/messages",
        {"Authorization": "Bearer placeholder", "X-Api-Key": "placeholder", "Host": "claude.unlimited",
         "Connection": "keep-alive", "X-Custom": "keep-me"},
        b"{}",
    )
    # The placeholder client-side Authorization is replaced by the stored
    # credential: never both present, never the placeholder leaking upstream.
    assert req.headers["Authorization"] == "Bearer real-token-xyz"
    assert "X-Api-Key" not in req.headers
    assert "Host" not in req.headers
    assert "Connection" not in req.headers
    assert req.headers["X-Custom"] == "keep-me"


def test_strips_accept_encoding_so_upstream_responds_uncompressed():
    # Clients send Accept-Encoding (gzip/br) and Anthropic honors it, but
    # usage_tracking.py cannot parse compressed SSE bytes as text, so every
    # such request would record no usage. Dropping the header costs nothing on
    # a loopback proxy; the client still gets a valid, if larger, response.
    p = oauth_profile()
    req = build_upstream_request(
        p, "real-token-xyz", "POST", "/v1/messages",
        {"Accept-Encoding": "gzip, deflate, br", "X-Custom": "keep-me"},
        b"{}",
    )
    assert "Accept-Encoding" not in req.headers
    assert req.headers["X-Custom"] == "keep-me"


def test_oauth_profile_gets_bearer_auth_header():
    p = oauth_profile()
    req = build_upstream_request(p, "sk-real-oauth-token", "POST", "/v1/messages", {}, b"{}")
    assert req.headers["Authorization"] == "Bearer sk-real-oauth-token"
    assert "x-api-key" not in req.headers


def test_api_key_mode_gets_x_api_key_header_not_bearer():
    p = api_profile(auth_mode="api_key")
    req = build_upstream_request(p, "sk-ant-real-key", "POST", "/v1/messages", {}, b"{}")
    assert req.headers["x-api-key"] == "sk-ant-real-key"
    assert "Authorization" not in req.headers


def test_bearer_mode_gateway_gets_authorization_bearer():
    p = api_profile(auth_mode="bearer")
    req = build_upstream_request(p, "gw-token", "POST", "/v1/messages", {}, b"{}")
    assert req.headers["Authorization"] == "Bearer gw-token"


def test_account_uuid_rewritten_inside_nested_user_id_json_for_oauth():
    # metadata.user_id is itself a JSON-encoded string containing account_uuid,
    # not the account uuid directly. Other fields inside it must survive
    # untouched.
    p = Profile(id="a", name="Personal Max", kind="oauth", account_uuid="the-real-account-uuid")
    inner = json.dumps({"account_uuid": "stale-uuid", "session_id": "keep-me"})
    body = json.dumps({"model": "claude-sonnet-4-5", "metadata": {"user_id": inner}}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body)
    parsed = json.loads(req.body)
    inner_parsed = json.loads(parsed["metadata"]["user_id"])
    assert inner_parsed["account_uuid"] == "the-real-account-uuid"
    assert inner_parsed["session_id"] == "keep-me"
    assert req.headers["Content-Length"] == str(len(req.body))


def test_account_uuid_not_rewritten_for_api_kind_profiles():
    p = api_profile()
    inner = json.dumps({"account_uuid": "whatever"})
    body = json.dumps({"metadata": {"user_id": inner}}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body)
    assert req.body == body  # untouched — api-kind has no account_uuid concept


def test_account_uuid_rewrite_skipped_outside_v1_messages_path():
    p = oauth_profile()
    inner = json.dumps({"account_uuid": "stale"})
    body = json.dumps({"metadata": {"user_id": inner}}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/oauth/token", {}, body)
    assert req.body == body


def test_non_json_body_passes_through_unchanged_not_a_crash():
    p = oauth_profile()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, b"not json at all")
    assert req.body == b"not json at all"


def test_body_missing_metadata_passes_through_unchanged():
    p = oauth_profile()
    body = json.dumps({"model": "x"}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body)
    assert req.body == body


def test_user_id_not_a_json_string_passes_through_unchanged():
    # user_id present but not the expected nested-JSON-string shape: pass it
    # through rather than guessing or corrupting it.
    p = oauth_profile()
    body = json.dumps({"metadata": {"user_id": "plain-string-not-json"}}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body)
    assert req.body == body


def test_user_id_json_without_account_uuid_key_passes_through_unchanged():
    p = oauth_profile()
    inner = json.dumps({"session_id": "abc"})  # no account_uuid key at all
    body = json.dumps({"metadata": {"user_id": inner}}).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body)
    assert req.body == body


def test_oversized_body_rejected():
    import pytest
    p = oauth_profile()
    with pytest.raises(RequestTooLarge):
        build_upstream_request(p, "tok", "POST", "/v1/messages", {}, b"x" * (MESSAGES_MAX_REQUEST_BYTES + 1))


def test_request_too_large_is_still_a_value_error_for_older_callers():
    assert issubclass(RequestTooLarge, ValueError)


def test_messages_api_limit_is_32mb_not_the_old_20mb():
    # Anthropic's documented Messages API limit. The old 20 MB cap rejected
    # image-heavy sessions the provider would have taken.
    assert MESSAGES_MAX_REQUEST_BYTES >= 32_000_000
    assert max_request_bytes(oauth_profile()) == MESSAGES_MAX_REQUEST_BYTES
    assert max_request_bytes(api_profile()) == MESSAGES_MAX_REQUEST_BYTES


def test_codex_limit_is_openais_50mb_payload():
    assert CODEX_MAX_REQUEST_BYTES >= 50_000_000
    assert max_request_bytes(Profile(id="c", name="C", kind="codex")) == CODEX_MAX_REQUEST_BYTES


def test_body_up_to_the_limit_builds_for_oauth_and_api():
    body = b"x" * MESSAGES_MAX_REQUEST_BYTES
    for p in (oauth_profile(), api_profile()):
        assert build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body).body == body


def test_check_request_size_boundary():
    import pytest
    p = oauth_profile()
    check_request_size(p, b"x" * MESSAGES_MAX_REQUEST_BYTES)
    with pytest.raises(RequestTooLarge):
        check_request_size(p, b"x" * (MESSAGES_MAX_REQUEST_BYTES + 1))


def test_request_model_reads_the_top_level_model_field():
    body = json.dumps({"model": "claude-opus-5", "messages": []}).encode()
    assert request_model(body) == "claude-opus-5"


def test_request_model_returns_none_for_non_json_body():
    assert request_model(b"not json") is None


def test_request_model_returns_none_when_field_missing_or_not_a_string():
    assert request_model(json.dumps({"messages": []}).encode()) is None
    assert request_model(json.dumps({"model": 5}).encode()) is None


def test_rewrite_model_swaps_the_field_preserving_everything_else():
    body = json.dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()
    rewritten = rewrite_model(body, "claude-sonnet-5")
    parsed = json.loads(rewritten)
    assert parsed["model"] == "claude-sonnet-5"
    assert parsed["messages"] == [{"role": "user", "content": "hi"}]


def test_rewrite_model_passes_through_non_json_body_unchanged():
    assert rewrite_model(b"not json", "claude-sonnet-5") == b"not json"


def test_rewrite_model_passes_through_body_without_model_field_unchanged():
    body = json.dumps({"messages": []}).encode()
    assert rewrite_model(body, "claude-sonnet-5") == body


def test_filter_response_headers_only_keeps_allowlist():
    headers = {
        "Anthropic-Ratelimit-Unified-5h-Utilization": "0.61",
        "Anthropic-Ratelimit-Unified-5h-Reset": "1787191800",
        "Set-Cookie": "should-never-pass-through",
        "X-Request-Id": "also-dropped",
    }
    filtered = filter_response_headers(headers)
    assert filtered == {
        "anthropic-ratelimit-unified-5h-utilization": "0.61",
        "anthropic-ratelimit-unified-5h-reset": "1787191800",
    }


# ---- Claude-side reasoning effort injection (output_config.effort) ----

def _body(**kw):
    base = {"model": "claude-fable-5-1", "messages": []}
    base.update(kw)
    return json.dumps(base).encode()


def test_claude_effort_is_injected_for_oauth_messages():
    req = build_upstream_request(oauth_profile(), "tok", "POST", "/v1/messages", {},
                                 _body(), claude_effort="xhigh")
    assert json.loads(req.body)["output_config"] == {"effort": "xhigh"}
    assert req.headers["Content-Length"] == str(len(req.body))


def test_claude_effort_is_injected_for_api_messages():
    req = build_upstream_request(api_profile(), "sk", "POST", "/v1/messages", {},
                                 _body(), claude_effort="high")
    assert json.loads(req.body)["output_config"]["effort"] == "high"


def test_claude_effort_merges_into_an_existing_output_config():
    body = _body(output_config={"format": {"type": "json"}})
    req = build_upstream_request(oauth_profile(), "tok", "POST", "/v1/messages", {},
                                 body, claude_effort="max")
    oc = json.loads(req.body)["output_config"]
    assert oc["effort"] == "max"
    assert oc["format"] == {"type": "json"}  # existing keys preserved


def test_claude_effort_none_leaves_the_body_untouched():
    body = _body()
    req = build_upstream_request(oauth_profile(), "tok", "POST", "/v1/messages", {},
                                 body, claude_effort=None)
    assert req.body == body
    assert "output_config" not in json.loads(req.body)


def test_claude_effort_is_not_injected_off_the_messages_path():
    body = _body()
    req = build_upstream_request(oauth_profile(), "tok", "POST", "/v1/count_tokens", {},
                                 body, claude_effort="high")
    assert "output_config" not in json.loads(req.body)


def test_claude_effort_and_account_uuid_rewrite_coexist():
    p = oauth_profile()
    body = json.dumps({
        "model": "claude-fable-5-1", "messages": [],
        "metadata": {"user_id": json.dumps({"account_uuid": "old", "session": "s"})},
    }).encode()
    req = build_upstream_request(p, "tok", "POST", "/v1/messages", {}, body, claude_effort="high")
    parsed = json.loads(req.body)
    assert parsed["output_config"]["effort"] == "high"
    assert json.loads(parsed["metadata"]["user_id"])["account_uuid"] == "acct-123"
    assert json.loads(parsed["metadata"]["user_id"])["session"] == "s"  # preserved


def test_claude_effort_passthrough_on_a_non_json_body():
    req = build_upstream_request(oauth_profile(), "tok", "POST", "/v1/messages", {},
                                 b"not json", claude_effort="high")
    assert req.body == b"not json"


# ---- which upstreams may be plain http -------------------------------------
# One rule, two enforcers: profiles.py refuses to SAVE a bad base_url and
# upstream.py refuses to SEND to one. They read the same module so a Profile
# can never be accepted and then rejected at request time.

import pytest

from claude_unlimited import net_scope, upstream
from claude_unlimited.proxy import UpstreamRequest


@pytest.mark.parametrize("url,allow_http", [
    ("http://localhost:11434/v1/messages", True),
    ("http://192.168.1.50:5566/v1/messages", True),
    ("http://mac-mini.local:8000/v1/messages", True),     # an mDNS name
    ("http://api.example.com/v1/messages", True),         # any host: the user's call
    ("https://api.anthropic.com/v1/messages", False),
])
def test_https_always_and_http_when_the_request_allows_it(url, allow_http, monkeypatch):
    opened = {}

    class _Conn:
        def __init__(self, host, port, timeout=None):
            opened.update(host=host, port=port)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            class _R:
                status = 200

                def getheaders(self):
                    return []

                def read(self, _n):
                    return b""
            return _R()

    monkeypatch.setattr(upstream.http.client, "HTTPConnection", _Conn)
    monkeypatch.setattr(upstream.http.client, "HTTPSConnection", _Conn)
    upstream.send(UpstreamRequest(method="POST", url=url, headers={}, body=b"{}", allow_http=allow_http))
    assert opened  # it got as far as opening a connection


def test_plain_http_is_refused_unless_the_request_allows_it():
    # Only an API profile's request carries allow_http; an OAuth token never
    # goes over plain http, whatever a hand-edited config says.
    with pytest.raises(ValueError):
        upstream.send(UpstreamRequest(method="POST", url="http://127.0.0.1/v1/messages",
                                      headers={}, body=b"{}"))


def test_only_an_api_profile_request_may_use_http():
    from claude_unlimited.config import Profile
    from claude_unlimited.proxy import build_upstream_request
    api = Profile(id="a", name="A", kind="api", base_url="http://mac-mini.local:8000")
    oauth = Profile(id="o", name="O", kind="oauth")
    assert build_upstream_request(api, "k", "POST", "/v1/messages", {}, b"{}").allow_http is True
    assert build_upstream_request(oauth, "t", "POST", "/v1/messages", {}, b"{}").allow_http is False


def test_the_save_rule_depends_on_the_profile_kind():
    for url in ("http://127.0.0.1:1234", "http://mac-mini.local:8000", "http://1.1.1.1",
                "https://api.anthropic.com"):
        net_scope.validate(url)                                   # API profile
    net_scope.validate("https://api.openai.com/v1", allow_http=False)
    with pytest.raises(net_scope.InvalidUpstreamURL):
        net_scope.validate("http://127.0.0.1:1234", allow_http=False)   # Codex


def test_a_dead_address_is_skipped_quickly_instead_of_eating_the_request_timeout(monkeypatch):
    # An address that never answers (IPv6 on a server listening on IPv4
    # only) used to hold the connect for the whole request timeout.
    import socket
    tried = []

    class _Sock:
        def __init__(self, family, kind, proto):
            self.family = family

        def settimeout(self, t):
            tried.append(("timeout", self.family, t))

        def bind(self, a):
            pass

        def connect(self, addr):
            if self.family == socket.AF_INET6:
                raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(upstream.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 5566, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 5566)),
    ])
    monkeypatch.setattr(upstream.socket, "socket", _Sock)
    sock = upstream._connect(("mac-mini.local", 5566), 120)
    assert sock.family == socket.AF_INET
    assert ("timeout", socket.AF_INET6, upstream.CONNECT_TIMEOUT_SECONDS) in tried
    assert tried[-1] == ("timeout", socket.AF_INET, 120)   # the request keeps its full timeout


def test_every_upstream_connection_uses_the_fast_connect():
    conn = upstream.open_connection("http", "mac-mini.local", 5566, 120)
    assert conn._create_connection is upstream._connect
    assert upstream.open_connection("https", "api.anthropic.com", None, 120)._create_connection is upstream._connect


def test_ipv6_link_local_is_tried_last(monkeypatch):
    import socket
    order = []

    class _Sock:
        def __init__(self, family, kind, proto):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            order.append(addr[0])
            raise OSError("refused")

        def close(self):
            pass

    monkeypatch.setattr(upstream.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 5566, 0, 14)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 5566, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 5566)),
    ])
    monkeypatch.setattr(upstream.socket, "socket", _Sock)
    with pytest.raises(OSError):
        upstream._connect(("mac-mini.local", 5566), 120)
    assert order == ["2001:db8::1", "192.0.2.10", "fe80::1"]


def test_relay_passes_each_sse_event_on_as_it_arrives():
    # Over a real chunked response: read(CHUNK_SIZE) would wait for 64 KB, so
    # the first small event would only arrive when the stream ended. The
    # server holds the stream open until the relay has yielded that event.
    import http.server
    import threading

    first_seen = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
        resp = upstream.send(UpstreamRequest(method="POST", url=f"http://127.0.0.1:{server.server_port}/v1/messages",
                                             headers={}, body=b"{}", allow_http=True), timeout=5)
        stream = iter(resp.body_chunks)
        assert next(stream) == b"data: one\n\n"
        first_seen.set()
        assert b"".join(stream) == b"data: two\n\n"
        resp.connection.close()
    finally:
        first_seen.set()
        server.server_close()
