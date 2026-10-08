"""HTTP 402 means the provider wants money, not that a quota window will reset
or a login went stale. The Profile is parked for an hour (not retried on every
request), the pool rotates past it, and a pool left with only such Profiles
answers 402 instead of a misleading "no eligible profile" 503.
"""

from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited import daemon
from claude_unlimited.config import Pool, Profile, load_pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.observation import BudgetUnavailable, UsageSnapshot, classify
from claude_unlimited.router import (PoolSnapshot, ProfileRuntime, ProfileState,
                                     observe, recover_expired_cooldowns)
from claude_unlimited.upstream import UpstreamResponse

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class _Conn:
    def close(self):
        pass


def _resp(status, headers=None, body=b"ok"):
    return UpstreamResponse(status=status, headers=headers or {},
                            body_chunks=iter([body]) if body else iter(()), connection=_Conn())


class _Secrets:
    def get_token(self, profile_id):
        return "tok-" + profile_id


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", _Secrets())
    monkeypatch.setattr(Gateway, "_maybe_check_oauth_credential", lambda self, p, rt: None)
    return tmp_path


def _rt(**kw):
    return ProfileRuntime(profile_id="a", priority=1, switch_threshold=98.0, automatic=True,
                          state=ProfileState.ELIGIBLE, **kw)


def test_402_classifies_as_budget_unavailable_whatever_the_headers():
    assert isinstance(classify(402, {}, NOW), BudgetUnavailable)
    assert isinstance(classify(402, {"anthropic-ratelimit-unified-5h-status": "rejected"}, NOW), BudgetUnavailable)


def test_observing_402_parks_the_profile_for_an_hour_and_flags_it():
    pool = PoolSnapshot(profiles=[_rt()], current_profile_id="a")
    after = observe(pool, "a", BudgetUnavailable(), NOW).profiles[0]
    assert after.state == ProfileState.COOLDOWN
    assert after.budget_unavailable is True
    assert after.cooldown_until == NOW + timedelta(hours=1)


def test_the_flag_clears_when_the_cooldown_lapses_and_on_a_success():
    parked = observe(PoolSnapshot(profiles=[_rt()], current_profile_id="a"), "a", BudgetUnavailable(), NOW)
    lapsed = recover_expired_cooldowns(parked, NOW + timedelta(hours=1, seconds=1)).profiles[0]
    assert lapsed.state == ProfileState.ELIGIBLE and lapsed.budget_unavailable is False

    ok = observe(parked, "a", UsageSnapshot(percent=10.0, resets_at=None, confidence="measured"), NOW).profiles[0]
    assert ok.budget_unavailable is False


def test_402_rotates_to_the_next_profile_and_leaves_the_first_flagged(env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    calls = []

    def transport(req):
        calls.append(req)
        return _resp(402) if len(calls) == 1 else _resp(200)  # only the very first call is refused

    gw = Gateway(transport=transport)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200 and result.profile_id == "b"
    a = gw._runtime["a"]
    assert a.state == ProfileState.COOLDOWN and a.budget_unavailable is True

    # Parked for an hour: the next request does not spend a call on it.
    before = len(calls)
    assert gw.handle("POST", "/v1/messages", {}, b"{}").profile_id == "b"
    assert len(calls) == before + 1


def test_a_pool_with_only_unfunded_profiles_answers_402_not_503(env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _resp(402))
    first = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert first.status == 402 and first.error == "provider_budget_unavailable"
    second = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert second.status == 402 and second.error == "provider_budget_unavailable"


def test_a_pinned_profile_that_is_unfunded_is_refused_with_402(env):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True),
    ]))
    gw = Gateway(transport=lambda req: _resp(402))
    pinned = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert pinned.status == 402  # relayed, never substituted with B
    again = gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="a")
    assert again.status == 402 and again.error == "provider_budget_unavailable"


def test_the_flag_survives_a_daemon_restart(env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: _resp(402))
    gw.handle("POST", "/v1/messages", {}, b"{}")
    gw._persist()

    restarted = Gateway(transport=lambda req: None)
    restarted._sync_snapshot(load_pool())
    assert restarted._runtime["a"].budget_unavailable is True
    assert restarted._runtime["a"].state == ProfileState.COOLDOWN


def test_dashboard_profile_row_exposes_the_flag():
    p = Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True)
    assert daemon._profile_to_public_dict(p, _rt(budget_unavailable=True))["budget_unavailable"] is True
    assert daemon._profile_to_public_dict(p, _rt())["budget_unavailable"] is False
    assert daemon._profile_to_public_dict(p)["budget_unavailable"] is False
