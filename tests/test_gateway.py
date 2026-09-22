import json
import time
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
    # And its usage is read straight away, not at the next scheduled read, so
    # the Dashboard fills in the moment the account is back.
    assert gw.take_usage_recheck_requests() == {"a"}
    assert gw.usage_recheck_wakeup.is_set()
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

    gw.runtime_snapshot()              # a poll schedules the check...
    gw.wait_for_credential_checks()    # ...which runs off the request thread
    snapshot = gw.runtime_snapshot()  # a bare sync, with no request and no manual re-auth

    assert refresh_calls == ["ref-a"]
    assert snapshot["a"].state == gateway_module.ProfileState.ELIGIBLE
    assert gw.take_usage_recheck_requests() == {"a"}   # its usage is read now
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


def test_a_leaked_in_flight_slot_self_heals_and_stops_pinning_used_now(pool_env, monkeypatch):
    # A request whose body never drains (client walked away, upstream hung)
    # leaves its Profile in _in_flight forever. Without a bound that pins "Used
    # now" and wedges is_idle indefinitely — actually observed as a Profile
    # stuck "Used now" 20+ minutes after its last request. in_flight_ids() and
    # seconds_since_last_activity() must ignore a slot older than the cap.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    fake_now = [1000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: fake_now[0])
    gw = Gateway(transport=lambda req: fake_response(200))

    with gw._lock:
        gw._in_flight.add("a")
        gw._in_flight_since["a"] = fake_now[0]  # a request starts and never drains

    assert "a" in gw.in_flight_ids()          # while fresh, correctly "Used now"
    assert gw.seconds_since_last_activity() == 0.0
    assert not gw.is_idle(60)

    fake_now[0] += Gateway._IN_FLIGHT_MAX_SECONDS + 1  # long past any real request
    assert "a" not in gw.in_flight_ids()      # the leaked slot stops counting
    assert gw.is_idle(60)                      # ...and no longer wedges the idle check


def test_force_active_clears_the_previous_profiles_used_now(pool_env):
    # A profile that just served shows "Used now" for its grace window. Taking
    # over with a DIFFERENT profile is an explicit "I'm on this one now", so the
    # one moved away from must stop showing "Used now" immediately rather than
    # lingering for the rest of the 15-minute grace (the reported bug: switching
    # to Codex left the old account stuck on "Used now").
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="codex", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                                                            "anthropic-ratelimit-unified-5h-reset": "1787191800"}))
    r = gw.handle("POST", "/v1/messages", {}, b"{}")  # "a" serves...
    list(r.body_chunks)  # ...drain so it moves to the grace window ("Used now")
    assert "a" in gw.in_flight_ids()

    assert gw.force_active("b") is True
    assert "a" not in gw.in_flight_ids()  # cleared on takeover, not lingering


def test_force_active_keeps_used_now_when_a_request_is_genuinely_in_flight(pool_env):
    # The grace clear must NOT drop a profile that has a request actually
    # streaming right now (e.g. another pinned terminal) — only the idle grace.
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="codex", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: fake_response(200))
    gw._current_profile_id = "a"
    gw._in_flight.add("a")  # a real request is being served on "a" right now

    assert gw.force_active("b") is True
    assert "a" in gw.in_flight_ids()  # still genuinely in use, stays lit


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


# --- a credential store that will not answer is transient, not "exhausted" ---


def test_a_keychain_failure_cools_the_profile_down_instead_of_exhausting_it(pool_env, monkeypatch):
    """A locked Keychain says nothing about an account's quota.

    This used to report QuotaExhausted(resets_at=None), and
    recover_expired_cooldowns() only recovers an EXHAUSTED Profile that HAS a
    reset time — so a Keychain locked for a few seconds removed a healthy
    account from rotation until the next daemon restart, labelled "exhausted"
    on the Dashboard."""
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    class LockedForA:
        def get_token(self, profile_id):
            if profile_id == "a":
                raise RuntimeError("User interaction is not allowed (keychain locked)")
            return "tok-b"

    monkeypatch.setattr(gateway_module, "secret_store", LockedForA())
    gw = Gateway(transport=lambda req: fake_response(
        200, {"anthropic-ratelimit-unified-5h-utilization": "0.1",
              "anthropic-ratelimit-unified-5h-reset": "1787191800"}))

    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200 and result.profile_id == "b"  # rotated past it

    runtime = gw.runtime_snapshot()["a"]
    assert runtime.state == ProfileState.COOLDOWN
    # The property that matters: a deadline exists, so it comes back by itself.
    assert runtime.cooldown_until is not None
    assert runtime.state != ProfileState.EXHAUSTED


# --- a codex account must be able to self-heal, exactly like an oauth one ---


def _codex_pool():
    save_pool(Pool(profiles=[Profile(
        id="a", name="Codex", kind="codex", auth_mode="chatgpt_subscription",
        automatic=True, enabled=True)]))


def test_an_auth_invalid_codex_profile_recovers_via_its_refresh_token(pool_env, monkeypatch):
    """choose() never picks an AUTH_INVALID Profile, and the per-request
    refresh only runs for a Profile that was picked — so without a sync-driven
    recovery a codex account was stranded until a manual re-auth, while the
    identical oauth case healed itself. AUTH_INVALID also survives a restart,
    so "restart the daemon" was not a way out either."""
    import claude_unlimited.openai_credential as openai_credential

    _codex_pool()
    stored = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="dead", refresh_token="ref-1", account_id="acct-1", id_token=None))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": stored}))
    # Patched before anything can call it: the real one reaches OpenAI.
    calls = []
    fresh = openai_credential.StoredOpenAICredential(
        access_token="alive", refresh_token="ref-2", account_id="acct-1", id_token=None)
    monkeypatch.setattr(gateway_module.openai_bridge, "refresh_now",
                        lambda profile_id, cred: calls.append(profile_id) or fresh)

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()  # populate _runtime before observing into it
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert gw._runtime["a"].state == ProfileState.AUTH_INVALID

    gw.runtime_snapshot()
    gw.wait_for_credential_checks()
    assert gw.runtime_snapshot()["a"].state == ProfileState.ELIGIBLE
    assert gw.take_usage_recheck_requests() == {"a"}   # codex too: read its usage now
    assert calls == ["a"], "the refresh was never attempted for a codex profile"


def test_a_codex_profile_whose_refresh_token_is_dead_stays_auth_invalid(pool_env, monkeypatch):
    """Recovery must never be faked: a revoked grant still needs a re-auth."""
    import claude_unlimited.openai_credential as openai_credential

    _codex_pool()
    stored = openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token="dead", refresh_token="revoked", account_id="acct-1", id_token=None))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": stored}))
    monkeypatch.setattr(gateway_module.openai_bridge, "refresh_now",
                        lambda profile_id, cred: None)

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()  # populate _runtime before observing into it
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert gw.runtime_snapshot()["a"].state == ProfileState.AUTH_INVALID


def test_a_codex_api_key_profile_is_left_alone(pool_env, monkeypatch):
    """A raw OpenAI API key has no refresh token; probing one would be a
    pointless keychain read on every sync tick."""
    save_pool(Pool(profiles=[Profile(
        id="a", name="Key", kind="codex", auth_mode="api_key",
        automatic=True, enabled=True)]))
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "sk-proj-x"}))
    monkeypatch.setattr(gateway_module.openai_bridge, "refresh_now",
                        lambda profile_id, cred: pytest.fail("refreshed an API-key profile"))

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()  # populate _runtime before observing into it
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert gw.runtime_snapshot()["a"].state == ProfileState.AUTH_INVALID


# --- transport failures that are not OSError -------------------------------


def test_a_malformed_upstream_response_rotates_instead_of_crashing(pool_env):
    """http.client.HTTPException is NOT an OSError. Catching only OSError let
    BadStatusLine/IncompleteRead — a proxy or middlebox returning garbage —
    escape handle() entirely: the client got a dropped connection with no HTTP
    status at all, and the Profile's in-flight slot leaked forever."""
    import http.client

    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    seen = []

    def transport(req):
        seen.append(req.headers.get("Authorization"))
        if len(seen) == 1:
            raise http.client.BadStatusLine("\x16\x03\x01")
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.2",
                                    "anthropic-ratelimit-unified-5h-reset": "1787191800"})

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200 and result.profile_id == "b"
    assert gw.runtime_snapshot()["a"].state == ProfileState.COOLDOWN


def test_an_unexpected_transport_error_does_not_leak_the_in_flight_slot(pool_env):
    """A leaked slot pins "Used now" on forever and wedges the idle check the
    auto-updater waits for, so the bug outlives the request that caused it."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))

    def transport(req):
        raise RuntimeError("a genuine bug, not a network failure")

    gw = Gateway(transport=transport)
    with pytest.raises(RuntimeError):
        gw.handle("POST", "/v1/messages", {}, b"{}")

    assert gw._in_flight == set()


def test_the_sync_loop_does_not_read_the_keychain_on_every_poll(pool_env, monkeypatch):
    """secret_store.get_token forks the `security` CLI on macOS, and this runs
    for every oauth Profile on every ~1s Dashboard poll, inside the lock every
    request also takes. The throttle used to be checked only inside
    _try_refresh — after the read — so it gated the network call but not the
    subprocess, contradicting its own field docstring."""
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))

    reads = []

    class CountingStore:
        def get_token(self, profile_id):
            reads.append(profile_id)
            return "tok"

    monkeypatch.setattr(gateway_module, "secret_store", CountingStore())
    monkeypatch.setattr(gateway_module.oauth_credential, "decode",
                        lambda blob: gateway_module.oauth_credential.StoredOAuthCredential(
                            access_token="t", refresh_token="r", expires_at=None))
    monkeypatch.setattr(gateway_module.oauth_credential, "is_expiring_soon", lambda cred: True)
    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token",
                        lambda token: (_ for _ in ()).throw(
                            gateway_module.oauth_login.OAuthLoginError("nope")))

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()
    after_first = len(reads)
    for _ in range(20):          # twenty more poll ticks, well inside the window
        gw.runtime_snapshot()

    assert len(reads) == after_first, (
        f"the keychain was read {len(reads) - after_first} more times across 20 polls")


def test_repeated_rate_limits_back_off_but_never_stop_retrying(pool_env, monkeypatch):
    """This used to give up permanently, and that was a deadlock.

    The streak is cleared only by a SUCCESSFUL refresh, and the give-up check
    returned before ever attempting one — so nothing could clear it and no
    retry ever happened again. The only exits were a manual re-auth or a daemon
    restart, which made a daemon left running the one case that could never
    recover. A real account sat given-up for seven hours and then expired.

    The escalating backoff is the protection: at the ceiling this is about four
    attempts a day, which is not noise against anyone's limiter."""
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

    waits = []
    for _ in range(20):
        try:
            gw._try_refresh("a", "refresh-tok")
        except gw_mod.oauth_login.OAuthLoginError:
            pass
        deadline = gw._refresh_check_not_before.get("a", now[0])
        waits.append(deadline - now[0])
        now[0] = deadline + 1

    # Every single attempt was made — none were refused by a permanent wall.
    assert len(calls) == 20, len(calls)
    # The wait escalates and then holds at the ceiling, rather than stopping.
    assert waits[0] < waits[3] <= Gateway._RATE_LIMIT_BACKOFF_CEILING_SECONDS
    assert waits[-1] == Gateway._RATE_LIMIT_BACKOFF_CEILING_SECONDS


def test_the_backoff_ceiling_keeps_retries_to_roughly_four_a_day(pool_env):
    """The number that has to stay defensible: this is what replaces the
    permanent give-up as the protection against hammering the endpoint."""
    per_day = 24 * 3600 / Gateway._RATE_LIMIT_BACKOFF_CEILING_SECONDS
    assert per_day <= 6, per_day


def test_a_stuck_account_does_not_hammer_the_token_endpoint(pool_env, monkeypatch):
    """This is what earned the 429s.

    A Profile that is already needs-re-auth bypasses the expiry check so it can
    self-heal — but it shared the 60s refresh cooldown, so it asked the token
    endpoint to refresh a dead credential 1,440 times a day. Anthropic rate
    limited it, which kept it needs-re-auth, which made it ask again in 60s.

    A recovery poll does not need to be fast. A preventive refresh does, and
    keeps the short interval."""
    import claude_unlimited.gateway as gw_mod

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    stored = gw_mod.oauth_credential.encode(gw_mod.oauth_credential.StoredOAuthCredential(
        access_token="dead", refresh_token="r", expires_at=None))
    monkeypatch.setattr(gw_mod, "secret_store", FakeSecretStore({"a": stored}))
    # NOT a 429: that path overwrites the cooldown with its own escalating
    # backoff, which would measure the wrong thing entirely.
    monkeypatch.setattr(gw_mod.oauth_login, "refresh_access_token",
                        lambda token: (_ for _ in ()).throw(
                            gw_mod.oauth_login.OAuthLoginError("server error", status_code=500)))

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()
    gw._observe("a", AuthInvalid(), datetime(2026, 1, 1, tzinfo=timezone.utc))

    now = [1000.0]
    monkeypatch.setattr(gw_mod.time, "monotonic", lambda: now[0])
    gw._refresh_check_not_before.clear()

    gw.runtime_snapshot()                      # one recovery attempt
    wait = gw._refresh_check_not_before["a"] - now[0]

    # Ten minutes, not one. At 60s this was ~1440 calls a day to a rate-limited
    # endpoint; the whole point is that a stuck account backs off.
    assert wait >= Gateway._REAUTH_RECOVERY_COOLDOWN_SECONDS
    assert 24 * 3600 / wait <= 150, f"{24 * 3600 / wait:.0f} attempts a day is still hammering"


def test_a_preventive_refresh_stays_responsive(pool_env, monkeypatch):
    """The long interval is for recovery only. A healthy token that is genuinely
    near expiry must still be refreshed promptly."""
    import claude_unlimited.gateway as gw_mod

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True, account_uuid="u")]))
    now = [1000.0]
    monkeypatch.setattr(gw_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gw_mod.oauth_login, "refresh_access_token",
                        lambda token: (_ for _ in ()).throw(
                            gw_mod.oauth_login.OAuthLoginError("nope", status_code=500)))
    gw = Gateway(transport=lambda req: fake_response(200))
    try:
        gw._try_refresh("a", "r")
    except gw_mod.oauth_login.OAuthLoginError:
        pass
    assert gw._refresh_check_not_before["a"] - now[0] == Gateway._REFRESH_CHECK_COOLDOWN_SECONDS


def test_the_gateway_lock_is_free_while_a_credential_refresh_runs(pool_env, monkeypatch):
    """The refresh reads the keychain (a subprocess on macOS) and calls the
    provider's token endpoint. Holding the gateway lock across that made every
    Dashboard and widget poll queue behind it — 20-45s on a loaded machine,
    with the widget falling back to "loading" each time."""
    import claude_unlimited.oauth_credential as oauth_credential
    import claude_unlimited.oauth_login as oauth_login
    import claude_unlimited.profiles as profile_repo

    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))

    class SettableFakeSecretStore(FakeSecretStore):
        def set_token(self, profile_id, token):
            self.tokens[profile_id] = token

    expiring = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token="tok-old", refresh_token="ref-a", expires_at=1))
    store = SettableFakeSecretStore({"a": expiring})
    monkeypatch.setattr(gateway_module, "secret_store", store)
    monkeypatch.setattr(profile_repo, "secret_store", store)

    gw = Gateway(transport=lambda req: fake_response(200))
    lock_free_during_refresh = []
    keychain_lock_free = []

    def watching_get_token(profile_id, _real=store.get_token):
        keychain_lock_free.append(gw._lock.acquire(blocking=False))
        if keychain_lock_free[-1]:
            gw._lock.release()
        return _real(profile_id)

    monkeypatch.setattr(store, "get_token", watching_get_token)

    def fake_refresh(refresh_token, timeout=30.0):
        # Whoever holds the lock here blocks every other caller for as long
        # as this network call takes.
        lock_free_during_refresh.append(gw._lock.acquire(blocking=False))
        if lock_free_during_refresh[-1]:
            gw._lock.release()
        return oauth_login.LoginTokens(access_token="tok-new", refresh_token="ref-a",
                                        expires_at=9_999_999_999_999)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", fake_refresh)

    gw.runtime_snapshot()            # schedules the check on a background thread
    gw.wait_for_credential_checks()  # and here it finishes, off this thread
    assert lock_free_during_refresh == [True], "the token refresh ran while holding the gateway lock"
    assert all(keychain_lock_free), "the keychain was read while holding the gateway lock"


def test_a_test_can_never_write_the_users_real_config(monkeypatch):
    """The backstop for what actually happened: a credential-check thread
    outlived its test, monkeypatch restored the real CONFIG_FILE, and the
    thread wrote its test pool over four live Profiles."""
    import claude_unlimited.config as config
    from pathlib import Path

    monkeypatch.setattr(config, "CONFIG_FILE", Path.home() / ".claude-unlimited" / "config.json")
    with pytest.raises(RuntimeError, match="refusing to write the real config"):
        config.save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth")]))


# ---- "everything is out of capacity" is said once, and says what it means --

def _exhausted_pool_gateway(monkeypatch, percent=99.0):
    """One enabled Profile, at its limit, with a known reset time."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True,
                                     switch_threshold=98.0)]))
    resets = datetime.now(timezone.utc) + timedelta(minutes=30)

    def transport(req):
        # The shape Anthropic actually sends when a window is spent: the
        # explicit "rejected" status, not just a high utilization.
        return fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected",
                                   "anthropic-ratelimit-unified-5h-utilization": str(percent / 100),
                                   "anthropic-ratelimit-unified-5h-reset": str(int(resets.timestamp()))})

    gw = Gateway(transport=transport)
    gw.handle("POST", "/v1/messages", {}, b"{}")  # the request that uses it up
    return gw, resets


def test_exhaustion_is_announced_once_however_many_requests_are_rejected(monkeypatch, pool_env):
    recorded = []
    monkeypatch.setattr("claude_unlimited.gateway.activity.record",
                        lambda category, text, meta=None: recorded.append(text))
    notified = []
    monkeypatch.setattr("claude_unlimited.gateway.notifications.notify_if_enabled",
                        lambda kind, title, body, settings: notified.append(body))
    gw, _ = _exhausted_pool_gateway(monkeypatch)

    for _ in range(25):
        result = gw.handle("POST", "/v1/messages", {}, b"{}")
        assert result.status == 503

    assert len([t for t in recorded if "out of capacity" in t]) == 1, recorded
    assert len(notified) == 1


def test_an_exhausted_pool_says_so_specifically_and_when_it_comes_back(pool_env):
    gw, resets = _exhausted_pool_gateway(None)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 503
    assert result.error == "all_profiles_exhausted"
    assert "all profiles are out of capacity" in result.error_detail.lower()
    assert f"{resets.astimezone():%H:%M}" in result.error_detail
    # Retry-After, so a client that honours it waits for real capacity.
    assert 0 < int(result.headers["retry-after"]) <= 30 * 60


def test_a_pool_that_is_merely_unreachable_is_not_reported_as_exhausted(pool_env):
    """A cooldown from a refused connection is not a quota problem, and must
    not tell the user they are out of capacity."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.error == "no_eligible_profile" and result.error_detail is None


def test_capacity_coming_back_is_recorded_and_re_arms_the_announcement(monkeypatch, pool_env):
    recorded = []
    monkeypatch.setattr("claude_unlimited.gateway.activity.record",
                        lambda category, text, meta=None: recorded.append(text))
    monkeypatch.setattr("claude_unlimited.gateway.notifications.notify_if_enabled",
                        lambda kind, title, body, settings: None)
    gw, _ = _exhausted_pool_gateway(monkeypatch)   # announces
    gw.handle("POST", "/v1/messages", {}, b"{}")   # stays quiet

    # The window reopens and a request succeeds: the runtime's reset time is
    # in the past by then, so the Profile recovers on the next request.
    gw._transport = lambda req: fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"})
    for rt in gw._runtime.values():
        rt.resets_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        rt.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert gw.handle("POST", "/v1/messages", {}, b"{}").status == 200
    assert any("Capacity is back" in t for t in recorded), recorded

    # Out again: the next outage is announced afresh, not swallowed.
    recorded.clear()
    gw._transport = lambda req: fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected",
                                                    "anthropic-ratelimit-unified-5h-utilization": "0.99"})
    gw.handle("POST", "/v1/messages", {}, b"{}")
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert len([t for t in recorded if "out of capacity" in t]) == 1, recorded


# ---- a request must never carry an empty text block upstream ---------------

def test_an_empty_text_block_in_the_history_is_dropped_before_forwarding(pool_env):
    """Anthropic rejects the whole request with "400 messages: text content
    blocks must be non-empty". A client re-sends its entire conversation every
    turn, so one empty block recorded in a session breaks every later request
    in it — including on accounts that never produced it."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    sent = []

    def transport(req):
        sent.append(json.loads(req.body))
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"})

    body = json.dumps({
        "model": "claude-sonnet-5",
        "system": [{"type": "text", "text": "be helpful"}, {"type": "text", "text": ""}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            # What the Codex bridge used to write: a reply that went straight
            # to a tool call, recorded as an empty text block.
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "  "},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        ],
    }).encode()

    result = Gateway(transport=transport).handle("POST", "/v1/messages", {}, body)
    assert result.status == 200

    forwarded = sent[0]
    assert forwarded["system"] == [{"type": "text", "text": "be helpful"}]
    # The message that was nothing but an empty block is gone; the one that
    # also carried a tool_use keeps it, so the tool_result still has its pair.
    assert [m["role"] for m in forwarded["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in forwarded["messages"][1]["content"]] == ["tool_use"]
    assert forwarded["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


def test_a_request_with_no_empty_blocks_is_forwarded_byte_for_byte(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    sent = []

    def transport(req):
        sent.append(req.body)
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"})

    body = json.dumps({"model": "claude-sonnet-5",
                       "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}).encode()
    Gateway(transport=transport).handle("POST", "/v1/messages", {}, body)
    assert sent[0] == body


# ---------------------------------------------------------------------------
# Issue #4 — returning to the highest-priority account after a failover.
# ---------------------------------------------------------------------------

_OK_HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.4",
               "anthropic-ratelimit-unified-5h-reset": "1787191800"}


def _two_profiles(**settings_kwargs):
    from claude_unlimited.config import Settings
    return Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ], settings=Settings(**settings_kwargs))


def _drain(result):
    """A Profile stays marked in-flight until its response body is read to the
    end, and an in-flight request means the pool is NOT idle. A test that
    leaves the body unread is a pool that never goes idle."""
    list(result.body_chunks or ())
    return result


def _fail_over_to_b(gw, calls):
    """Drive the pool onto the fallback, then let A recover."""
    result = _drain(gw.handle("POST", "/v1/messages", {}, b"{}"))
    assert result.profile_id == "b", "the failover itself did not happen"
    # A's quota window has reset; it is eligible again.
    gw._runtime["a"].state = gateway_module.ProfileState.ELIGIBLE
    gw._runtime["a"].resets_at = None


def _transport_failing_a_once():
    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        if calls[-1] == "Bearer tok-a" and len(calls) == 1:
            return fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"})
        return fake_response(200, _OK_HEADERS)

    return transport, calls


def test_the_pool_stays_on_the_fallback_by_default(pool_env):
    """The sticky behaviour issue #4 reports is deliberate — it keeps the
    prompt cache warm — so it must be what happens with the setting off."""
    save_pool(_two_profiles())
    transport, calls = _transport_failing_a_once()
    gw = Gateway(transport=transport)
    _fail_over_to_b(gw, calls)

    assert _drain(gw.handle("POST", "/v1/messages", {}, b"{}")).profile_id == "b"


def test_an_idle_pool_returns_to_the_preferred_account_when_asked(pool_env):
    save_pool(_two_profiles(return_to_preferred=True))
    transport, calls = _transport_failing_a_once()
    gw = Gateway(transport=transport)
    _fail_over_to_b(gw, calls)

    # Nothing has been served for longer than the idle gate.
    for pid in list(gw._last_active):
        gw._last_active[pid] -= gw._RETURN_TO_PREFERRED_IDLE_SECONDS + 1
    assert _drain(gw.handle("POST", "/v1/messages", {}, b"{}")).profile_id == "a"


def test_a_busy_pool_is_never_moved_mid_session(pool_env):
    """The whole risk in this feature is yanking a live session off its
    account. The idle gate is what prevents it."""
    save_pool(_two_profiles(return_to_preferred=True))
    transport, calls = _transport_failing_a_once()
    gw = Gateway(transport=transport)
    _fail_over_to_b(gw, calls)

    # The last request was seconds ago, not ten minutes.
    assert gw.handle("POST", "/v1/messages", {}, b"{}").profile_id == "b"


def test_take_over_outranks_the_return(pool_env):
    """A standing manual override is an explicit choice made now; the setting
    is a preference made once."""
    save_pool(_two_profiles(return_to_preferred=True))
    transport, calls = _transport_failing_a_once()
    gw = Gateway(transport=transport)
    _fail_over_to_b(gw, calls)
    gw._manual_profile_id = "b"
    for pid in list(gw._last_active):
        gw._last_active[pid] -= gw._RETURN_TO_PREFERRED_IDLE_SECONDS + 1

    assert gw.handle("POST", "/v1/messages", {}, b"{}").profile_id == "b"


def test_the_return_never_moves_down_the_priority_order(pool_env):
    """It only ever goes UP. With the preferred account still spent, an idle
    pool must stay exactly where it is rather than churn."""
    save_pool(_two_profiles(return_to_preferred=True))
    transport, calls = _transport_failing_a_once()
    gw = Gateway(transport=transport)
    result = _drain(gw.handle("POST", "/v1/messages", {}, b"{}"))
    assert result.profile_id == "b"
    # A is left EXHAUSTED this time — no recovery.
    for pid in list(gw._last_active):
        gw._last_active[pid] -= gw._RETURN_TO_PREFERRED_IDLE_SECONDS + 1

    assert gw.handle("POST", "/v1/messages", {}, b"{}").profile_id == "b"


# ---------------------------------------------------------------------------
# Rejection-triggered usage re-read.
#
# Polling gives up to ~10 minutes of staleness, during which requests for a
# spent model keep landing on the account that cannot serve them. A 429 the
# ACCOUNT-level windows cannot explain is the event that says "the data is
# already wrong", so the usage endpoint is worth re-reading now.
# ---------------------------------------------------------------------------

def _rate_limited_transport():
    def transport(req):
        # 429 with no retry-after and no "rejected" status: a ShortRateLimit,
        # not a quota exhaustion.
        return fake_response(429, {})
    return transport


def test_a_429_the_account_windows_cannot_explain_asks_for_a_usage_reread(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=_rate_limited_transport())
    gw.runtime_snapshot()
    with gw._lock:
        # Comfortably below the switch threshold: nothing we already know
        # explains a refusal.
        gw._runtime["a"].last_usage_percent = 12.0
        gw._runtime["a"].last_usage_percent_7d = 20.0

    _drain(gw.handle("POST", "/v1/messages", {}, b"{}"))
    assert gw.take_usage_recheck_requests() == {"a"}


def test_a_429_on_an_account_near_its_threshold_asks_for_nothing(pool_env):
    """The numbers we already hold explain this one, so a re-read would tell
    us nothing — and an account being rate-limited is the last one to send
    extra requests to."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=_rate_limited_transport())
    gw.runtime_snapshot()
    with gw._lock:
        gw._runtime["a"].last_usage_percent = 97.0   # inside the approaching band
        gw._runtime["a"].last_usage_percent_7d = 10.0

    _drain(gw.handle("POST", "/v1/messages", {}, b"{}"))
    assert gw.take_usage_recheck_requests() == set()


def test_a_successful_request_asks_for_nothing(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: fake_response(200, _OK_HEADERS))
    _drain(gw.handle("POST", "/v1/messages", {}, b"{}"))
    assert gw.take_usage_recheck_requests() == set()


def test_at_most_one_reread_per_profile_per_interval(pool_env):
    """The point is to replace a scheduled read, not to add a burst of them."""
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=_rate_limited_transport())
    gw.runtime_snapshot()
    with gw._lock:
        gw._runtime["a"].last_usage_percent = 5.0

    assert gw._request_usage_recheck("a") is True
    assert gw._request_usage_recheck("a") is False     # immediately after
    assert gw.take_usage_recheck_requests() == {"a"}
    # Draining does NOT reset the clock: a honoured request still counts.
    assert gw._request_usage_recheck("a") is False
    assert gw.take_usage_recheck_requests() == set()


def test_the_interval_eventually_allows_another_reread(pool_env, monkeypatch):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=_rate_limited_transport())
    assert gw._request_usage_recheck("a") is True

    base = real_time.monotonic()
    monkeypatch.setattr(gateway_module.time, "monotonic",
                        lambda: base + gw._USAGE_RECHECK_MIN_INTERVAL + 1)
    assert gw._request_usage_recheck("a") is True


def test_draining_is_one_shot(pool_env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1,
                                      automatic=True, enabled=True)]))
    gw = Gateway(transport=_rate_limited_transport())
    gw._request_usage_recheck("a")
    assert gw.take_usage_recheck_requests() == {"a"}
    assert gw.take_usage_recheck_requests() == set()


def test_a_rejected_model_is_only_looked_up_once(pool_env):
    """A single-model endpoint (a local model server, a one-deployment
    gateway) answers 404 for a Claude model name. The first request learns it
    and retries; every later one sends the default model straight away instead
    of paying the same 404 + retry forever."""
    save_pool(Pool(profiles=[Profile(id="a", name="Local", kind="api", priority=1, automatic=True,
                                     enabled=True, base_url="http://127.0.0.1:5566",
                                     default_model="local-model-1")]))
    sent = []

    def transport(req):
        sent.append(json.loads(req.body)["model"])
        if sent[-1] != "local-model-1":
            return fake_response(404, body=b'{"error":{"message":"Model not found"}}')
        return fake_response(200)

    gw = Gateway(transport=transport)
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()

    first = gw.handle("POST", "/v1/messages", {}, body)
    assert first.status == 200
    assert sent == ["claude-haiku-4-5", "local-model-1"]   # asked, refused, retried

    second = gw.handle("POST", "/v1/messages", {}, body)
    assert second.status == 200
    assert sent[2:] == ["local-model-1"]                    # no wasted round trip


def test_a_multi_model_endpoint_is_not_second_guessed(pool_env):
    """Only a model this endpoint actually refused is rewritten. A gateway
    that serves several models keeps getting exactly what was asked for."""
    save_pool(Pool(profiles=[Profile(id="a", name="Gateway", kind="api", priority=1, automatic=True,
                                     enabled=True, base_url="https://gw.example",
                                     default_model="fallback-model")]))
    sent = []

    def transport(req):
        sent.append(json.loads(req.body)["model"])
        return fake_response(200)

    gw = Gateway(transport=transport)
    for model in ("claude-opus-5", "claude-sonnet-5"):
        body = json.dumps({"model": model, "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        assert gw.handle("POST", "/v1/messages", {}, body).status == 200
    assert sent == ["claude-opus-5", "claude-sonnet-5"]
