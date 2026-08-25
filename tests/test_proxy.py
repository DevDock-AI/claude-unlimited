import json

from claude_unlimited.config import Profile
from claude_unlimited.proxy import (
    ANTHROPIC_DEFAULT_BASE_URL,
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
    with pytest.raises(ValueError):
        build_upstream_request(p, "tok", "POST", "/v1/messages", {}, b"x" * 21_000_000)


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
