import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
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


def test_get_profiles_empty_list(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/profiles")
    assert status == 200
    assert body["profiles"] == []


def test_post_profile_without_csrf_token_is_rejected(running_server):
    base, _ = running_server
    status, body = _request(f"{base}/api/profiles", "POST", {"name": "X", "kind": "oauth", "credential": "sk-ant-12345678"})
    assert status == 403
    assert body["error"] == "csrf"


def test_post_profile_with_wrong_csrf_token_is_rejected(running_server):
    base, _ = running_server
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "X", "kind": "oauth", "credential": "sk-ant-12345678"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert status == 403


def test_full_crud_roundtrip_with_valid_csrf(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}

    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "Personal Max", "kind": "oauth", "credential": "sk-ant-12345678", "account_uuid": "acct-test"},
        headers=headers,
    )
    assert status == 201
    profile_id = body["profile"]["id"]
    assert "credential" not in body["profile"]

    status, body = _request(f"{base}/api/profiles")
    assert len(body["profiles"]) == 1

    status, body = _request(f"{base}/api/profiles/{profile_id}", "PATCH", {"priority": 2}, headers=headers)
    assert status == 200
    assert body["profile"]["priority"] == 2

    status, body = _request(f"{base}/api/profiles/{profile_id}", "DELETE", headers=headers)
    assert status == 200

    status, body = _request(f"{base}/api/profiles")
    assert body["profiles"] == []


def test_update_credential_rotates_the_stored_secret(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST",
                             {"credential": "sk-ant-rotated-key"}, headers=headers)
    assert status == 200

    # The stored shape is oauth_credential-encoded, not the raw string —
    # decoding it is what actually proves the rotation happened.
    import claude_unlimited.oauth_credential as oauth_credential
    decoded = oauth_credential.decode(profile_repo.secret_store.get_token(profile_id))
    assert decoded.access_token == "sk-ant-rotated-key"


def test_update_credential_requires_csrf(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST", {"credential": "sk-ant-new-key"})
    assert status == 403


def test_update_credential_404s_for_unknown_profile(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(f"{base}/api/profiles/does-not-exist/credential", "POST",
                             {"credential": "sk-ant-new-key"}, headers=headers)
    assert status == 404


def test_update_credential_rejects_too_short_a_value(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(
        f"{base}/api/profiles", "POST",
        {"name": "API key", "kind": "api", "credential": "sk-ant-original-key"},
        headers=headers,
    )
    profile_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{profile_id}/credential", "POST", {"credential": "short"}, headers=headers)
    assert status == 400


def test_invalid_host_header_is_rejected(running_server):
    base, token = running_server
    status, body = _request(f"{base}/api/profiles", headers={"Host": "evil.example.com"})
    assert status == 400
    assert body["error"] == "invalid_host"


def test_no_cors_header_ever_sent(running_server):
    base, _ = running_server
    req = urllib.request.Request(f"{base}/api/status")
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.headers.get("Access-Control-Allow-Origin") is None


def test_responses_never_cached(running_server):
    base, _ = running_server
    req = urllib.request.Request(f"{base}/api/status")
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.headers.get("Cache-Control") == "no-store"


def test_update_endpoint_reports_state_without_touching_the_network(running_server, monkeypatch):
    """GET /api/update is a cheap read of the last known state: dashboard
    polling must never trigger an outbound request."""
    from claude_unlimited import updater

    called = []
    monkeypatch.setattr(updater, "run_update_cycle", lambda *a, **k: called.append(1))

    base, _ = running_server
    status, body = _request(f"{base}/api/update")

    assert status == 200
    assert body["current_version"] == daemon.__version__
    assert "update_mode" in body and "available" in body
    assert not called, "GET /api/update must not run a check"


def test_add_profile_accepts_the_dashboard_form_payload(running_server):
    """The Add Profile form always sends forced_for_subagents. create_profile
    once had no such parameter, so every profile added from the Dashboard
    failed with a 400 — no test sent the form's real payload."""
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    form = {"kind": "api", "credential": "sk-ant-api-12345678", "switch_threshold": 98,
            "automatic": True, "auth_mode": "api_key"}

    status, body = _request(f"{base}/api/profiles", "POST",
                            {**form, "name": "Console", "priority": 1, "forced_for_subagents": False}, headers=headers)
    assert status == 201, body
    status, body = _request(f"{base}/api/profiles", "POST",
                            {**form, "name": "Subagents", "priority": 2, "forced_for_subagents": True}, headers=headers)
    assert status == 201, body
    assert body["profile"]["forced_for_subagents"] is True

    status, body = _request(f"{base}/api/profiles")
    assert [p["live_agents"] for p in body["profiles"]] == [0, 0]


def test_leave_on_fable_limit_is_on_the_wire_for_every_kind_and_settable(running_server):
    """The Add Profile form and the detail panel both send it; /api/profiles
    reports it for oauth, codex and api alike (the UI hides it for api)."""
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(f"{base}/api/profiles", "POST",
                            {"kind": "api", "credential": "sk-ant-api-12345678", "switch_threshold": 98,
                             "automatic": True, "auth_mode": "api_key", "name": "Key", "priority": 1,
                             "forced_for_subagents": False, "leave_on_fable_limit": False}, headers=headers)
    assert status == 201, body
    assert body["profile"]["leave_on_fable_limit"] is False
    status, body = _request(f"{base}/api/profiles", "POST",
                            {"kind": "codex", "credential": "sk-openai-12345678", "switch_threshold": 98,
                             "automatic": True, "auth_mode": "api_key", "name": "GPT", "priority": 2,
                             "forced_for_subagents": False, "leave_on_fable_limit": True}, headers=headers)
    assert status == 201, body
    assert body["profile"]["leave_on_fable_limit"] is True
    codex_id = body["profile"]["id"]

    status, body = _request(f"{base}/api/profiles/{codex_id}", "PATCH",
                            {"leave_on_fable_limit": False}, headers=headers)
    assert status == 200, body
    assert body["profile"]["leave_on_fable_limit"] is False

    status, body = _request(f"{base}/api/profiles")
    assert [(p["name"], p["leave_on_fable_limit"]) for p in body["profiles"]] == [("Key", False), ("GPT", False)]


def test_presence_needs_csrf_and_marks_the_user_active(running_server, monkeypatch, tmp_path):
    import claude_unlimited.usage_probe as usage_probe
    scheduler = usage_probe.Scheduler(state_file=tmp_path / "ups.json")
    monkeypatch.setattr(daemon, "_usage_probe", scheduler)
    base, token = running_server

    status, _ = _request(f"{base}/api/presence", "POST", {})
    assert status == 403 and scheduler.is_active() is False

    status, body = _request(f"{base}/api/presence", "POST", {}, headers={"X-CSRF-Token": token})
    assert status == 200 and body == {"active": True}
    assert scheduler.is_active() is True


def test_keep_usage_fresh_is_on_by_default_round_trips_and_must_be_boolean(running_server):
    base, token = running_server
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    status, body = _request(f"{base}/api/settings")
    assert body["settings"]["keep_usage_fresh"] is True

    status, body = _request(f"{base}/api/settings", "PATCH", {"keep_usage_fresh": False}, headers=headers)
    assert status == 200 and body["settings"]["keep_usage_fresh"] is False

    status, _ = _request(f"{base}/api/settings", "PATCH", {"keep_usage_fresh": "no"}, headers=headers)
    assert status == 400



def test_status_exposes_an_unsmoothed_idle_signal(running_server, monkeypatch):
    """Issue #3: `in_use_now` carries a 15-minute grace so the Dashboard light
    does not flicker, which makes it a false busy signal for scripts.
    /api/status carries the real numbers instead."""
    base, _ = running_server

    status, body = _request(f"{base}/api/status")
    assert status == 200
    # Nothing served since this daemon started: idle for an unknown length of
    # time, not "idle for 0 seconds".
    assert body["idle_seconds"] is None
    assert body["serving_now"] == []

    gw = daemon._gateway
    with gw._lock:
        gw._in_flight.add("p-live")
        gw._in_flight_since["p-live"] = __import__("time").monotonic()

    status, body = _request(f"{base}/api/status")
    assert body["serving_now"] == ["p-live"]
    assert body["idle_seconds"] == 0.0


def test_status_idle_signal_ignores_a_leaked_in_flight_slot(running_server):
    """A slot older than _IN_FLIGHT_MAX_SECONDS is a hung request, not live
    use — it must not pin the pool as busy forever."""
    import time as _time

    base, _ = running_server
    gw = daemon._gateway
    with gw._lock:
        gw._in_flight.add("p-leaked")
        gw._in_flight_since["p-leaked"] = _time.monotonic() - (gw._IN_FLIGHT_MAX_SECONDS + 1)
        gw._last_active["p-leaked"] = _time.monotonic() - 30.0

    status, body = _request(f"{base}/api/status")
    assert body["serving_now"] == []
    assert body["idle_seconds"] >= 29.0
