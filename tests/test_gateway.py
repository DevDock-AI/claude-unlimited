import time as real_time
from datetime import datetime, timedelta, timezone


import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.observation import AuthInvalid, UsageSnapshot
from claude_unlimited.router import ProfileState
from claude_unlimited.upstream import UpstreamResponse


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def fake_response(status, headers=None, body=b"ok"):
    def chunks():
        if body:
            yield body

    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "tok-a", "b": "tok-b"}))
    return tmp_path


def test_single_healthy_profile_serves_request(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.4",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200
    assert result.profile_id == "a"


def test_no_profiles_returns_503(pool_env):
    save_pool(Pool(profiles=[]))
    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503
    assert result.error == "no_eligible_profile"


def test_rotates_transparently_on_quota_exhausted_without_client_seeing_it(pool_env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        if len(calls) == 1:
            return fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"})
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.5",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200  # the client never sees the 429 at all
    assert result.profile_id == "b"
    assert len(calls) == 2
    assert calls[0] == "Bearer tok-a"
    assert calls[1] == "Bearer tok-b"


def test_sticky_after_success_prefers_same_profile_next_request(pool_env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.4",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    r1 = gw.handle("POST", "/v1/messages", {}, b"{}")
    r2 = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert r1.profile_id == "a"
    assert r2.profile_id == "a"  # sticky, priority-1 profile still healthy


def test_no_retry_after_escalation_streak_survives_intervening_sync_calls(pool_env):
    # _sync_snapshot reconstructs ProfileRuntime on every call, including every
    # Dashboard poll tick. It must carry consecutive_unretryable_failures over,
    # or router.py's no-Retry-After escalation is reset to 0 within a second of
    # being incremented.
    import datetime as dt_module

    from claude_unlimited.observation import ShortRateLimit

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(429, {}))
    now = dt_module.datetime.now(dt_module.timezone.utc)
    gw.runtime_snapshot()  # seed self._runtime["a"] before observing directly on it

    # First failure, matching how observation.py classifies a bare 429 (no
    # unified-5h/7d "-status: rejected", no retry-after). Observed directly
    # rather than through handle() so the test doesn't depend on wall-clock
    # cooldown expiry; _sync_snapshot is exercised identically either way.
    gw._observe("a", ShortRateLimit(retry_after_seconds=None), now)
    assert gw.runtime_snapshot()["a"].consecutive_unretryable_failures == 1

    # Intervening Dashboard poll ticks: _sync_snapshot calls with no new
    # observation, as happens while a Profile sits in COOLDOWN.
    for _ in range(5):
        gw.runtime_snapshot()
    assert gw.runtime_snapshot()["a"].consecutive_unretryable_failures == 1  # NOT reset to 0

    gw._observe("a", ShortRateLimit(retry_after_seconds=None), now)
    assert gw.runtime_snapshot()["a"].consecutive_unretryable_failures == 2  # correctly escalated, not restarted at 1


def test_used_now_stays_visible_for_the_full_grace_window_not_just_seconds(pool_env, monkeypatch):
    # "Used now" means "the Profile an active session is currently using", not
    # "a request completed a moment ago". A normal gap between calls (thinking
    # time, a long tool call, reading a response) must not flicker it off; it
    # stays true until the session switches or goes idle for the grace window.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    fake_now = [1000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: fake_now[0])

    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(result.body_chunks)  # fully drain, as the server does while streaming;
                              # this triggers _wrap_with_in_flight_clear's completion

    fake_now[0] += gateway_module.Gateway._USED_NOW_GRACE_SECONDS - 1
    assert "a" in gw.in_flight_ids()  # still well within the grace window

    fake_now[0] += 2  # now just past the full grace window
    assert "a" not in gw.in_flight_ids()


def test_used_now_clears_immediately_on_a_real_rotation_switch(pool_env, monkeypatch):
    # Rotating away from a Profile must clear "Used now" right away rather than
    # linger for the rest of the grace window: the active session has moved on.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    fake_now = [1000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: fake_now[0])
    # Computed relative to now, not the fixed constant used elsewhere in this
    # file: recover_expired_cooldowns uses wall-clock time inside handle(), so
    # a hardcoded calendar date eventually reads as already-passed and would
    # recover "a" to ELIGIBLE before the second handle() reaches choose().
    future_reset = str(int(real_time.time()) + 3600)

    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        if len(calls) == 1:
            # "a" is draining — its very next request rotates away to "b".
            return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.99",
                                        "anthropic-ratelimit-unified-5h-reset": future_reset})
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                    "anthropic-ratelimit-unified-5h-reset": future_reset})

    gw = Gateway(transport=transport)
    r1 = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(r1.body_chunks)
    assert "a" in gw.in_flight_ids()

    fake_now[0] += 1  # a moment later, well inside the grace window
    r2 = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(r2.body_chunks)
    assert r2.profile_id == "b"  # rotation actually switched

    assert "a" not in gw.in_flight_ids()  # cleared immediately, not lingering
    assert "b" in gw.in_flight_ids()


def test_disabled_profile_never_selected(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=False)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503


def test_non_automatic_profile_not_auto_selected_when_no_current(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=False, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503


def test_all_profiles_exhausted_returns_503_not_infinite_loop(pool_env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"}))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503


def test_transport_network_error_rotates_to_next_profile_instead_of_crashing(pool_env):
    # A socket timeout, refused connection, or DNS failure against one
    # Profile's upstream must not crash the request thread with no response.
    # It should cooldown that Profile and try the next eligible one, exactly
    # like a 503/529 ProviderUnavailable response would.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        if len(calls) == 1:
            raise TimeoutError("upstream did not respond")  # an OSError subclass
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.4",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200  # the client never sees the network failure
    assert result.profile_id == "b"
    assert len(calls) == 2


def test_transport_network_error_on_every_profile_returns_503_not_unhandled_exception(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503
    assert result.error == "no_eligible_profile"


def test_oversized_body_fails_fast_instead_of_looping_every_profile(pool_env):
    # build_upstream_request's 20MB cap raises ValueError for ANY profile, so
    # retrying the next one just repeats the error. Fail with a 400 immediately
    # rather than looping MAX_ROTATION_ATTEMPTS times and surfacing a
    # misleading "no eligible profile" 503.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    calls = []
    gw = Gateway(transport=lambda req: (calls.append(1), fake_response(200))[1])
    oversized_body = b"x" * 20_000_001
    result = gw.handle("POST", "/v1/messages", {}, oversized_body)
    assert result.status == 400
    assert result.error == "bad_request"
    assert calls == []  # failed before any network call


def test_reauthenticating_a_profile_clears_stuck_auth_invalid_state(pool_env):
    # An AUTH_INVALID Profile is excluded from choose()'s candidates and has no
    # time-based recovery, unlike COOLDOWN/EXHAUSTED. Refreshing its credential
    # (CLI `login`, "Import current login", re-pasting a token) must be enough
    # to retry it on the next poll or request, with no disable/enable toggle.
    import claude_unlimited.profiles as profile_repo

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class RejectingThenAcceptingTransport:
        def __init__(self):
            self.calls = 0

        def __call__(self, req):
            self.calls += 1
            if self.calls == 1:
                return fake_response(401)
            return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                        "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    transport = RejectingThenAcceptingTransport()
    gw = Gateway(transport=transport)

    rejected = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert rejected.status == 401  # the 401 is forwarded to the client unchanged
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID

    still_stuck = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert still_stuck.status == 503  # re-attempting with no change does NOT self-heal
    assert transport.calls == 1  # AUTH_INVALID profiles aren't even retried

    class FakeSecretStoreWithSet(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    fake_store = FakeSecretStoreWithSet({"a": "tok-a"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(profile_repo, "secret_store", fake_store)
    try:
        profile_repo.update_credential("a", "fresh-token-after-reauth")
    finally:
        monkeypatch.undo()

    recovered = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert recovered.status == 200
    assert recovered.profile_id == "a"
    assert transport.calls == 2  # the refreshed credential got tried


def test_auth_invalid_oauth_profile_self_recovers_via_refresh_token_on_sync(pool_env, monkeypatch):
    # choose() never selects an AUTH_INVALID Profile, so the per-request
    # proactive refresh (_maybe_refresh_credential) can never run for one.
    # runtime_snapshot() — called by the Dashboard poll and the daemon's
    # background thread — must self-heal such a Profile from its refresh_token
    # alone, with no manual re-auth.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login
    import claude_unlimited.profiles as profile_repo

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class SettableFakeSecretStore(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    fake_store = SettableFakeSecretStore({"a": "tok-a"})
    monkeypatch.setattr(gateway_module, "secret_store", fake_store)
    monkeypatch.setattr(profile_repo, "secret_store", fake_store)

    gw = Gateway(transport=lambda req: fake_response(401))
    rejected = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert rejected.status == 401
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID

    # Now the credential behind it actually has a working refresh_token —
    # e.g. the access token's real TTL simply expired while this Profile
    # sat idle, not a truly dead/revoked grant.
    expired_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-a", refresh_token="ref-a", expires_at=1))
    fake_store.tokens["a"] = expired_blob

    refresh_calls = []

    def fake_refresh(refresh_token, timeout=30.0):
        refresh_calls.append(refresh_token)
        return oauth_login.LoginTokens(access_token="tok-recovered", refresh_token="ref-a-rotated",
                                        expires_at=9_999_999_999_999)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    snapshot = gw.runtime_snapshot()  # a bare sync, with no request and no manual re-auth

    assert refresh_calls == ["ref-a"]
    assert snapshot["a"].state == gateway_module.ProfileState.ELIGIBLE
    persisted = oauth_credential.decode(fake_store.tokens["a"])
    assert persisted.access_token == "tok-recovered"


def test_rate_limited_refresh_backs_off_far_longer_than_a_normal_retry(pool_env, monkeypatch):
    # Retrying a rate-limited token endpoint on the normal cooldown keeps the
    # limiter tripped, since each attempt is another strike against it. A 429
    # must push the next allowed attempt far past the normal cooldown.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    dead_for_now_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-a", refresh_token="ref-a", expires_at=1))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": dead_for_now_blob}))

    refresh_calls = []

    def fake_refresh(refresh_token, timeout=30.0):
        refresh_calls.append(refresh_token)
        raise oauth_login.OAuthLoginError("Token refresh failed (HTTP 429): rate limited", status_code=429)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    fake_now = [1000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: fake_now[0])

    gw = Gateway(transport=lambda req: fake_response(401))
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID
    assert len(refresh_calls) == 1  # the first attempt, which was 429'd

    # Well past the normal cooldown but nowhere near the 429 backoff, so no
    # retry is allowed yet.
    fake_now[0] += gateway_module.Gateway._REFRESH_CHECK_COOLDOWN_SECONDS + 30
    gw.runtime_snapshot()
    assert len(refresh_calls) == 1  # still no second attempt

    # Now past the full rate-limit backoff window — a retry is due again.
    fake_now[0] += gateway_module.Gateway._RATE_LIMIT_BACKOFF_SECONDS
    gw.runtime_snapshot()
    assert len(refresh_calls) == 2


def test_auth_invalid_profile_with_dead_refresh_token_stays_auth_invalid(pool_env, monkeypatch):
    # A refresh against a revoked or expired refresh_token must not fake a
    # recovery; the Profile still needs a manual re-auth.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class SettableFakeSecretStore(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    dead_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-a", refresh_token="ref-dead", expires_at=1))
    fake_store = SettableFakeSecretStore({"a": dead_blob})
    monkeypatch.setattr(gateway_module, "secret_store", fake_store)

    gw = Gateway(transport=lambda req: fake_response(401))
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID

    def fake_refresh(refresh_token, timeout=30.0):
        raise oauth_login.OAuthLoginError("refresh_token revoked")

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID


def test_idle_eligible_oauth_profile_is_refreshed_by_sync_alone_no_request_needed(pool_env, monkeypatch):
    # An ELIGIBLE Profile nearing expiry must be refreshed by runtime_snapshot()
    # itself (Dashboard poll, background thread). _maybe_refresh_credential runs
    # inside handle(), which never fires for a Profile nothing is routing to.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login
    import claude_unlimited.profiles as profile_repo

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class SettableFakeSecretStore(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    expiring_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-old", refresh_token="ref-a", expires_at=1))
    fake_store = SettableFakeSecretStore({"a": expiring_blob})
    monkeypatch.setattr(gateway_module, "secret_store", fake_store)
    monkeypatch.setattr(profile_repo, "secret_store", fake_store)

    refresh_calls = []

    def fake_refresh(refresh_token, timeout=30.0):
        refresh_calls.append(refresh_token)
        return oauth_login.LoginTokens(access_token="tok-new-fresh", refresh_token="ref-a-rotated",
                                        expires_at=9_999_999_999_999)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    gw = Gateway(transport=lambda req: fake_response(200))
    snapshot = gw.runtime_snapshot()  # no handle() call; a bare sync must trigger the refresh

    assert refresh_calls == ["ref-a"]
    assert snapshot["a"].state == gateway_module.ProfileState.ELIGIBLE
    persisted = oauth_credential.decode(fake_store.tokens["a"])
    assert persisted.access_token == "tok-new-fresh"


def test_expiring_oauth_credential_is_proactively_refreshed_before_use(pool_env, monkeypatch):
    # A Profile can look healthy while holding an access token that is already
    # at or near expiry. gateway.py must notice an expiring stored credential
    # and refresh it via its refresh_token BEFORE sending the request, not
    # after a 401 has already forced the Profile into AUTH_INVALID.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login
    import claude_unlimited.profiles as profile_repo

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    expired_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-old-expired", refresh_token="ref-a", expires_at=1))  # epoch ms — long past

    class SettableFakeSecretStore(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    fake_store = SettableFakeSecretStore({"a": expired_blob})
    monkeypatch.setattr(gateway_module, "secret_store", fake_store)
    monkeypatch.setattr(profile_repo, "secret_store", fake_store)

    refresh_calls = []

    def fake_refresh(refresh_token, timeout=30.0):
        refresh_calls.append(refresh_token)
        return oauth_login.LoginTokens(access_token="tok-new-fresh", refresh_token="ref-a-rotated",
                                        expires_at=9_999_999_999_999)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    seen_auth_headers = []

    def transport(req):
        seen_auth_headers.append(req.headers.get("Authorization"))
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert refresh_calls == ["ref-a"]  # refreshed with the old refresh_token, before sending the request
    assert seen_auth_headers == ["Bearer tok-new-fresh"]  # the outbound request used the new token

    persisted = oauth_credential.decode(fake_store.tokens["a"])
    assert persisted.access_token == "tok-new-fresh"
    assert persisted.refresh_token == "ref-a-rotated"  # refreshed credential is persisted for next time too


def test_oauth_credential_with_no_known_expiry_is_never_proactively_refreshed(pool_env, monkeypatch):
    # A plain credential string carries no expiry info and no refresh_token, so
    # it must never trigger a refresh attempt.
    import claude_unlimited.oauth_login as oauth_login

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "plain-legacy-token"}))

    def fake_refresh(refresh_token, timeout=30.0):
        raise AssertionError("must not be called when there's no known refresh_token/expiry")

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    seen_auth_headers = []

    def transport(req):
        seen_auth_headers.append(req.headers.get("Authorization"))
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert seen_auth_headers == ["Bearer plain-legacy-token"]


def test_fresh_oauth_blob_credential_sends_the_decoded_access_token_not_the_raw_blob(pool_env, monkeypatch):
    # A blob-shaped credential that is not expiring soon must still send the
    # DECODED access_token as the Bearer credential. The raw stored string is a
    # JSON object, and sending it produces a 401 indistinguishable from a
    # genuinely bad credential.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    fresh_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-fresh", refresh_token="ref-a", expires_at=9_999_999_999_999))  # far future
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": fresh_blob}))

    def fake_refresh(refresh_token, timeout=30.0):
        raise AssertionError("must not refresh a credential that isn't expiring soon")

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    seen_auth_headers = []

    def transport(req):
        seen_auth_headers.append(req.headers.get("Authorization"))
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200
    assert seen_auth_headers == ["Bearer tok-fresh"]  # NOT the raw JSON blob


def test_refresh_failure_falls_back_to_the_decoded_access_token_not_the_raw_blob(pool_env, monkeypatch):
    # Same guarantee as above, on the refresh-attempted-and-failed path.
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    expired_blob = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-stale", refresh_token="ref-a", expires_at=1))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": expired_blob}))

    def fake_refresh(refresh_token, timeout=30.0):
        raise oauth_login.OAuthLoginError("refresh endpoint unavailable")

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    seen_auth_headers = []

    def transport(req):
        seen_auth_headers.append(req.headers.get("Authorization"))
        return fake_response(401)

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 401
    assert seen_auth_headers == ["Bearer tok-stale"]  # NOT the raw JSON blob


def test_force_active_overrides_priority_and_makes_the_profile_sticky(pool_env):
    # Profile "a" would normally win automatic selection over "b". Take over
    # must pick "b" anyway and keep it picked on following requests.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))

    assert gw.force_active("b") is True
    r1 = gw.handle("POST", "/v1/messages", {}, b"{}")
    r2 = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert r1.profile_id == "b"
    assert r2.profile_id == "b"  # sticky, not a one-shot pin


def test_force_active_overrides_a_draining_profile_past_its_threshold(pool_env):
    # A Profile past its switch_threshold is DRAINING and normally excluded
    # from new selection. Take over must still make it active.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.995",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    gw.handle("POST", "/v1/messages", {}, b"{}")  # pushes it into DRAINING (>98% default threshold)
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DRAINING

    assert gw.force_active("a") is True
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.ELIGIBLE


def test_force_active_returns_false_for_a_disabled_profile(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=False)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    assert gw.force_active("a") is False
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DISABLED  # untouched, not resurrected


def test_force_active_returns_false_for_an_unknown_profile(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    assert gw.force_active("does-not-exist") is False


def test_forced_profile_id_always_picks_that_profile_even_when_a_lower_priority_one_would_normally_win(pool_env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="b")
    assert result.status == 200
    assert result.profile_id == "b"


def test_forced_profile_id_never_moves_the_shared_current_profile_pointer(pool_env):
    # Pinning one terminal must not disturb other concurrent, rotating
    # sessions' view of the active Profile. A forced request updates its own
    # Profile's runtime state, so Dashboard usage stays accurate, but never
    # touches self._current_profile_id or fires a "Rotated" notification.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    gw.handle("POST", "/v1/messages", {}, b"{}")  # normal request: "a" becomes current
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.ELIGIBLE

    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="b")
    assert result.profile_id == "b"

    unforced_again = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert unforced_again.profile_id == "a"  # the forced "b" request never became sticky


def test_forced_profile_id_bypasses_cooldown_and_draining_like_take_over_does(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.995",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    gw.handle("POST", "/v1/messages", {}, b"{}")  # pushes "a" into DRAINING
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DRAINING

    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert result.status == 200
    assert result.profile_id == "a"


def test_forced_profile_id_returns_the_real_quota_exhausted_response_instead_of_rotating(pool_env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        return fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert result.status == 429  # the upstream response, not a synthesized 503
    assert result.profile_id == "a"
    assert len(calls) == 1  # never tried "b": pinned means pinned


def test_forced_profile_id_refuses_immediately_when_it_needs_reauth(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class CountingTransport:
        def __init__(self):
            self.calls = 0

        def __call__(self, req):
            self.calls += 1
            return fake_response(401)

    transport = CountingTransport()
    gw = Gateway(transport=transport)
    gw.handle("POST", "/v1/messages", {}, b"{}")  # a 401 pushes "a" into AUTH_INVALID
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.AUTH_INVALID
    assert transport.calls == 1

    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert result.status == 503
    assert result.error == "forced_profile_needs_reauth"
    assert transport.calls == 1  # no retry against a credential already known dead


def test_forced_profile_id_for_a_disabled_profile_returns_a_clear_error(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=False)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert result.status == 503
    assert result.error == "forced_profile_disabled"


def test_forced_profile_id_for_an_unknown_profile_returns_a_clear_error(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="does-not-exist")
    assert result.status == 503
    assert result.error == "forced_profile_missing"


def test_unsupported_model_on_api_profile_retries_with_its_default_model(pool_env):
    import json
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      default_model="claude-haiku-4-5")]))

    def transport(req):
        model = json.loads(req.body)["model"]
        if model != "claude-haiku-4-5":
            return fake_response(403, body=b'{"error":{"type":"permission_error"}}')
        return fake_response(200, body=b"ok-with-default-model")

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, json.dumps({"model": "claude-opus-5", "messages": []}).encode())
    assert result.status == 200
    assert result.profile_id == "a"
    # The discarded first 403 must not mark the Profile AUTH_INVALID: nothing
    # was wrong with the credential.
    assert gw.runtime_snapshot()["a"].state != gateway_module.ProfileState.AUTH_INVALID


def test_model_fallback_does_not_apply_to_oauth_profiles(pool_env):
    import json
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      default_model="claude-haiku-4-5")]))
    calls = []

    def transport(req):
        calls.append(req.body)
        return fake_response(403, body=b'{"error":{"type":"permission_error"}}')

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, json.dumps({"model": "claude-opus-5", "messages": []}).encode())
    assert len(calls) == 1  # no retry: default_model fallback is API-kind only
    assert result.status == 403


def test_model_fallback_not_attempted_when_already_using_default_model(pool_env):
    import json
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      default_model="claude-haiku-4-5")]))
    calls = []

    def transport(req):
        calls.append(req.body)
        return fake_response(403, body=b'{"error":{"type":"permission_error"}}')

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, json.dumps({"model": "claude-haiku-4-5", "messages": []}).encode())
    assert len(calls) == 1  # retrying with an identical body would just reproduce the same failure
    assert result.status == 403


def test_model_fallback_not_attempted_when_profile_has_no_default_model(pool_env):
    import json
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True)]))
    calls = []

    def transport(req):
        calls.append(req.body)
        return fake_response(403, body=b'{"error":{"type":"permission_error"}}')

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, json.dumps({"model": "claude-opus-5", "messages": []}).encode())
    assert len(calls) == 1
    assert result.status == 403


def test_api_profile_over_its_token_threshold_is_excluded_from_rotation(pool_env):
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True, token_threshold=1000),
        Profile(id="b", name="B", kind="api", priority=2, automatic=True, enabled=True),
    ]))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 900, "output_tokens": 200})  # 1100 >= 1000

    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.profile_id == "b"  # "a" is over budget, skipped despite higher priority
    # EXHAUSTED, not DRAINING: a hard token cap that has already been passed is
    # exhausted. DRAINING's "near threshold" wording fits OAuth's soft %
    # crossing, not this.
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.EXHAUSTED


def test_api_profile_under_its_token_threshold_stays_eligible(pool_env):
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      token_threshold=1000)]))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 50})  # 150 < 1000

    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.profile_id == "a"


def test_token_threshold_has_no_effect_on_oauth_profiles(pool_env):
    # token_threshold is an api-kind concept only. Even when set (e.g. left over
    # from a kind change), it must never gate an OAuth Profile.
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      token_threshold=100)]))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 900, "output_tokens": 200})

    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.profile_id == "a"


def test_token_threshold_does_not_override_auth_invalid_or_disabled(pool_env):
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=False,
                                      token_threshold=1000)]))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 50})

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.handle("POST", "/v1/messages", {}, b"{}")
    # Still DISABLED, not EXHAUSTED: a budget check must never resurrect a
    # Profile the user explicitly turned off.
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DISABLED


def test_raising_switch_threshold_instantly_recovers_a_draining_oauth_profile(pool_env):
    # Raising the threshold must recover a DRAINING Profile on its own, with no
    # disable/enable toggle and no new request.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      switch_threshold=50.0)]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.6",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    gw.handle("POST", "/v1/messages", {}, b"{}")  # 60% >= 50% threshold -> DRAINING
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DRAINING

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      switch_threshold=80.0)]))
    # A bare re-check (the Dashboard's own poll of runtime_snapshot()) recovers
    # it, with no new request.
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.ELIGIBLE


def test_lowering_switch_threshold_below_last_usage_does_not_falsely_recover(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      switch_threshold=50.0)]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.6",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DRAINING

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True,
                                      switch_threshold=40.0)]))  # still below last-observed 60%
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.DRAINING


def test_raising_token_threshold_instantly_recovers_an_exhausted_api_profile(pool_env):
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      token_threshold=100)]))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 50})  # 150 >= 100
    gw = Gateway(transport=lambda req: fake_response(200))
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.EXHAUSTED

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      token_threshold=1000)]))
    assert gw.runtime_snapshot()["a"].state == gateway_module.ProfileState.ELIGIBLE


def test_usage_numbers_and_current_profile_survive_a_restart(pool_env):
    # A fresh Gateway() — which is what a daemon restart is — must rehydrate
    # usage numbers and the current Profile from what the previous instance
    # persisted, rather than showing a blank "not yet observed" Dashboard while
    # the quota window is still open.
    import time as time_module

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    future_epoch = int(time_module.time()) + 3600  # 1h ahead of now, not a fixed constant
    gw1 = Gateway(transport=lambda req: fake_response(200, {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-5h-reset": str(future_epoch),
    }))
    gw1.handle("POST", "/v1/messages", {}, b"{}")
    assert gw1.runtime_snapshot()["a"].last_usage_percent == 42.0

    gw2 = Gateway(transport=lambda req: fake_response(200))  # simulates the daemon restarting
    restored = gw2.runtime_snapshot()["a"]
    assert restored.last_usage_percent == 42.0
    assert restored.resets_at is not None
    assert restored.state == gateway_module.ProfileState.ELIGIBLE  # state is not restored, only the numbers

    from claude_unlimited.router import choose
    from claude_unlimited.router import PoolSnapshot
    from datetime import datetime, timezone
    decision = choose(PoolSnapshot(profiles=list(gw2.runtime_snapshot().values()), current_profile_id="a"),
                       datetime.now(timezone.utc))
    assert decision.profile_id == "a"  # current_profile_id was restored too


def test_expired_usage_number_is_not_restored_after_a_restart(pool_env):
    # A window whose reset time has already passed must not be restored:
    # showing a used-percent past its own reset is wrong, not merely stale.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    past_epoch = 1_700_000_000  # long past, regardless of when this test runs
    gw1 = Gateway(transport=lambda req: fake_response(200, {
        "anthropic-ratelimit-unified-5h-utilization": "0.91",
        "anthropic-ratelimit-unified-5h-reset": str(past_epoch),
    }))
    gw1.handle("POST", "/v1/messages", {}, b"{}")

    gw2 = Gateway(transport=lambda req: fake_response(200))
    restored = gw2.runtime_snapshot()["a"]
    assert restored.last_usage_percent is None
    assert restored.resets_at is None


def test_rate_limited_refresh_backs_off_further_each_time(pool_env, monkeypatch):
    """A flat retry interval never lets a persistently rate-limited endpoint
    recover — it keeps arriving at the same rate indefinitely."""
    import claude_unlimited.gateway as gw_mod
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))

    def always_rate_limited(_token):
        raise gw_mod.oauth_login.OAuthLoginError("rate limited", status_code=429)

    monkeypatch.setattr(gw_mod.oauth_login, "refresh_access_token", always_rate_limited)

    waits = []
    now = [1000.0]
    monkeypatch.setattr(gw_mod.time, "monotonic", lambda: now[0])
    for _ in range(4):
        try:
            gw._try_refresh("a", "refresh-tok")
        except gw_mod.oauth_login.OAuthLoginError:
            pass
        waits.append(gw._refresh_check_not_before["a"] - now[0])
        now[0] = gw._refresh_check_not_before["a"]  # jump to when it is allowed again

    assert waits == sorted(waits), waits
    assert waits[1] > waits[0] and waits[2] > waits[1], waits
    assert waits[-1] <= Gateway._RATE_LIMIT_BACKOFF_CEILING_SECONDS


def test_refresh_gives_up_after_repeated_rate_limits(pool_env, monkeypatch):
    """Once the endpoint has refused for hours, only re-authentication helps.
    Continuing to ask is noise against someone else's rate limiter."""
    import claude_unlimited.gateway as gw_mod
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))

    calls = []

    def always_rate_limited(_token):
        calls.append(1)
        raise gw_mod.oauth_login.OAuthLoginError("rate limited", status_code=429)

    monkeypatch.setattr(gw_mod.oauth_login, "refresh_access_token", always_rate_limited)
    now = [1000.0]
    monkeypatch.setattr(gw_mod.time, "monotonic", lambda: now[0])

    for _ in range(20):
        try:
            gw._try_refresh("a", "refresh-tok")
        except gw_mod.oauth_login.OAuthLoginError:
            pass
        now[0] = gw._refresh_check_not_before.get("a", now[0]) + 1

    assert len(calls) == Gateway._MAX_CONSECUTIVE_RATE_LIMITED_REFRESHES, len(calls)
    assert gw._try_refresh("a", "refresh-tok") is None  # stays given up


def test_a_successful_refresh_clears_the_rate_limit_streak(pool_env, monkeypatch):
    import claude_unlimited.gateway as gw_mod
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    gw._refresh_rate_limited_streak["a"] = 3

    monkeypatch.setattr(gw_mod.oauth_login, "refresh_access_token",
                        lambda _t: gw_mod.oauth_login.LoginTokens(
                            access_token="new", refresh_token="r2", expires_at=None))
    assert gw._try_refresh("a", "refresh-tok").access_token == "new"
    assert "a" not in gw._refresh_rate_limited_streak


def _runtime_file(tmp_path, monkeypatch):
    import claude_unlimited.gateway as gw_mod
    path = tmp_path / "runtime_state.json"
    monkeypatch.setattr(gw_mod.runtime_state, "RUNTIME_STATE_FILE", path)
    return path


def test_needs_reauth_survives_a_restart(pool_env, monkeypatch):
    """A rejected credential does not repair itself by restarting. Coming back
    as 'healthy' would misreport it and fail on the next request anyway."""
    import claude_unlimited.gateway as gw_mod
    _runtime_file(pool_env, monkeypatch)
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    gw.runtime_snapshot()
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert gw.runtime_snapshot()["a"].state == ProfileState.AUTH_INVALID
    gw._persist()

    revived = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    assert revived.runtime_snapshot()["a"].state == ProfileState.AUTH_INVALID


def test_usage_numbers_survive_a_restart(pool_env, monkeypatch):
    import claude_unlimited.gateway as gw_mod
    _runtime_file(pool_env, monkeypatch)
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    gw.runtime_snapshot()
    future = datetime.now(timezone.utc) + timedelta(hours=4)
    gw._observe("a", UsageSnapshot(percent=5.0, resets_at=future, confidence="measured"),
                datetime.now(timezone.utc))
    gw._persist()

    revived = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    assert revived.runtime_snapshot()["a"].last_usage_percent == 5.0


def test_a_profile_disabled_while_down_comes_back_disabled(pool_env, monkeypatch):
    """Configuration always wins over restored state."""
    _runtime_file(pool_env, monkeypatch)
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    gw.runtime_snapshot()
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    gw._persist()

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=False, account_uuid="u")]))
    revived = Gateway(transport=lambda req: (_ for _ in ()).throw(AssertionError("no transport")))
    assert revived.runtime_snapshot()["a"].state == ProfileState.DISABLED


def test_is_idle_reflects_real_usage():
    gw = Gateway(transport=lambda req: None)
    assert gw.is_idle(600) is True          # nothing served yet
    gw._last_active["a"] = real_time.monotonic()
    assert gw.is_idle(600) is False         # just used
    assert gw.seconds_since_last_activity() < 5
    gw._in_flight.add("a")
    assert gw.seconds_since_last_activity() == 0.0
