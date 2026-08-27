"""A newly added Profile shows real usage immediately.

Usage percentages come from response headers, so before this a fresh Profile
had no runtime state and the Dashboard showed it blank until that account
happened to serve a request — the first thing a person sees after connecting
an account was the least accurate.
"""
from datetime import datetime, timezone

import claude_unlimited.daemon as daemon
from claude_unlimited.config import Profile
from claude_unlimited.observation import UsageSnapshot


def _oauth_profile(**kw):
    defaults = dict(id="p1", name="Acc", kind="oauth", auth_mode="oauth")
    defaults.update(kw)
    return Profile(**defaults)


def _codex_profile(**kw):
    defaults = dict(id="c1", name="Codex", kind="codex", auth_mode="chatgpt_subscription")
    defaults.update(kw)
    return Profile(**defaults)


def test_a_ping_records_usage_for_an_oauth_profile(monkeypatch):
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append((pid, obs)))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_oauth_profile(), {
        "ok": True, "status": 200,
        "headers": {
            "anthropic-ratelimit-unified-5h-utilization": "0.42",
            "anthropic-ratelimit-unified-7d-utilization": "0.10",
        },
    })

    assert len(observed) == 1
    pid, obs = observed[0]
    assert pid == "p1"
    assert isinstance(obs, UsageSnapshot)
    assert obs.percent == 42


def test_a_codex_ping_corrects_a_plan_the_jwt_got_wrong(monkeypatch):
    # The plan claim inside a codex credential can disagree with what the
    # backend reports; the header is the authority.
    updates = []
    monkeypatch.setattr(daemon.profile_repo, "update_profile",
                        lambda pid, **ch: updates.append((pid, ch)))
    monkeypatch.setattr(daemon._gateway, "_observe", lambda pid, obs, now: None)
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_codex_profile(plan="free"), {
        "ok": True, "status": 200, "headers": {"x-codex-plan-type": "pro"},
    })

    assert updates == [("c1", {"plan": "pro"})]


def test_a_matching_plan_is_not_rewritten(monkeypatch):
    updates = []
    monkeypatch.setattr(daemon.profile_repo, "update_profile",
                        lambda pid, **ch: updates.append(pid))
    monkeypatch.setattr(daemon._gateway, "_observe", lambda pid, obs, now: None)
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_codex_profile(plan="pro"), {
        "ok": True, "status": 200, "headers": {"x-codex-plan-type": "pro"},
    })
    assert updates == []


def test_a_ping_with_no_response_records_nothing(monkeypatch):
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(pid))
    daemon._record_ping(_oauth_profile(), {"ok": False})
    assert observed == []


def test_priming_never_raises_when_the_account_is_unreachable(monkeypatch):
    def boom(profile_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(daemon.connection_test, "test_connection", boom)
    monkeypatch.setattr(daemon.profile_repo, "list_profiles", lambda: [_oauth_profile()])
    daemon._prime_profile("p1")  # must not raise — the Profile exists either way


def test_priming_skips_a_disabled_profile(monkeypatch):
    called = []
    monkeypatch.setattr(daemon.connection_test, "test_connection",
                        lambda pid: called.append(pid))
    monkeypatch.setattr(daemon.profile_repo, "list_profiles",
                        lambda: [_oauth_profile(enabled=False)])
    daemon._prime_profile("p1")
    assert called == []  # never spend a request on an account that is switched off


def test_enabling_a_disabled_profile_that_is_automatic_triggers_a_refresh():
    # The reported bug: a deactivated Profile had auto-rotation switched on and
    # nothing was fetched, because priming refuses to ping a disabled Profile
    # and enabling it later did not re-trigger.
    assert daemon._should_prime_after_update(
        _oauth_profile(enabled=False, automatic=True),
        _oauth_profile(enabled=True, automatic=True)) is True


def test_automating_an_enabled_profile_triggers_a_refresh():
    assert daemon._should_prime_after_update(
        _oauth_profile(enabled=True, automatic=False),
        _oauth_profile(enabled=True, automatic=True)) is True


def test_automating_a_disabled_profile_does_not_ping_it():
    # Still not a rotation candidate, and priming would refuse anyway.
    assert daemon._should_prime_after_update(
        _oauth_profile(enabled=False, automatic=False),
        _oauth_profile(enabled=False, automatic=True)) is False


def test_turning_on_auto_rotation_triggers_a_refresh():
    # Becoming a rotation candidate is the point where usage starts deciding
    # routing, and the Profile may never have served a request.
    assert daemon._should_prime_after_update(
        _oauth_profile(automatic=False), _oauth_profile(automatic=True)) is True


def test_resaving_an_already_automatic_profile_does_not_reping():
    assert daemon._should_prime_after_update(
        _oauth_profile(automatic=True), _oauth_profile(automatic=True)) is False


def test_turning_auto_rotation_off_does_not_ping():
    assert daemon._should_prime_after_update(
        _oauth_profile(automatic=True), _oauth_profile(automatic=False)) is False


def test_an_unrelated_edit_does_not_ping():
    assert daemon._should_prime_after_update(
        _oauth_profile(automatic=False, name="a"),
        _oauth_profile(automatic=False, name="b")) is False


# NOTE: an earlier version of this file asserted that a 401 from a probe was
# ALWAYS discarded. That was the wrong fix for the right problem: the probe was
# mis-resolving oauth credentials (sending a JSON blob as the bearer token) and
# manufacturing false 401s, so suppressing the result hid the symptom. The
# resolution bug is fixed and covered by tests/test_connection_test_credentials.py
# — which is where that protection actually lives. Suppressing AuthInvalid here
# only hid genuinely dead credentials from the Dashboard, which is what the user
# hit: "test connection says re-auth needed, but the status never changes".


def test_a_rate_limited_probe_is_also_discarded(monkeypatch):
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(obs))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_oauth_profile(), {"ok": False, "status": 429, "headers": {}})
    assert observed == []


def test_a_genuine_auth_failure_is_recorded(monkeypatch):
    # The user-reported bug: an expired account probed via Test connection or
    # Fetch info returned 401, but the Profile stayed "healthy" because the
    # probe discarded everything that was not a usage reading.
    from claude_unlimited.observation import AuthInvalid
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(obs))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_oauth_profile(), {"ok": False, "status": 401, "headers": {}})
    assert len(observed) == 1 and isinstance(observed[0], AuthInvalid)


def test_a_transient_failure_is_still_discarded(monkeypatch):
    # A 5xx or a short rate-limit says something about this one small request
    # or about the provider right now — not about the account. One probe is
    # weak evidence, and acting on it would drop a healthy account.
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(obs))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    for status in (429, 500, 503, 418):
        daemon._record_ping(_oauth_profile(), {"ok": False, "status": status, "headers": {}})
    assert observed == []
