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


def test_a_failed_probe_never_marks_a_profile_auth_invalid(monkeypatch):
    # Pressing "Test connection" once marked a healthy account "needs re-auth":
    # the probe sent a mis-resolved credential, got a 401, and the recording
    # applied it. A probe may only improve what is known about a Profile.
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(obs))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_oauth_profile(), {"ok": False, "status": 401, "headers": {}})
    assert observed == []


def test_a_rate_limited_probe_is_also_discarded(monkeypatch):
    observed = []
    monkeypatch.setattr(daemon._gateway, "_observe",
                        lambda pid, obs, now: observed.append(obs))
    monkeypatch.setattr(daemon._gateway, "_persist", lambda: None)

    daemon._record_ping(_oauth_profile(), {"ok": False, "status": 429, "headers": {}})
    assert observed == []
