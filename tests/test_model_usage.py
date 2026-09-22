"""Per-model weekly windows — "how much Fable have I got left?". The
data path from the usage endpoint to the dashboard, so the tests pin the data path end to end and that real traffic
can never erase or fabricate them.
"""
import time as time_module
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.gateway as gateway_module
import claude_unlimited.usage_probe as usage_probe
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.observation import ModelWindow, UsageSnapshot
from claude_unlimited.router import PoolSnapshot, ProfileRuntime, observe

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

# The row as the endpoint returned it on 2026-09-16.
FABLE_ROW = {"kind": "weekly_scoped", "group": "weekly", "percent": 26, "severity": "normal",
             "is_active": True, "resets_at": "2026-09-22T13:59:59.818864+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}}


# ---- parsing --------------------------------------------------------------

def test_the_real_fable_row_becomes_one_window():
    body = {"limits": [{"kind": "session", "percent": 3}, {"kind": "weekly_all", "percent": 23}, FABLE_ROW]}
    assert usage_probe.anthropic_model_windows(body) == (
        ModelWindow(name="Fable", percent=26.0,
                    resets_at=datetime(2026, 9, 22, 13, 59, 59, 818864, tzinfo=timezone.utc), active=True),)


@pytest.mark.parametrize("body", [
    None, {}, {"limits": None}, {"limits": "x"},
    {"limits": [{"kind": "weekly_scoped", "percent": 10}]},                                     # no scope
    {"limits": [{**FABLE_ROW, "scope": {"model": {"display_name": ""}}}]},                     # empty name
    {"limits": [{**FABLE_ROW, "percent": "26"}]},                                              # percent as string
    {"limits": [{**FABLE_ROW, "percent": True}]},                                              # bool is not a number
    {"limits": [{**FABLE_ROW, "kind": "weekly_all"}]},                                         # not model-scoped
])
def test_unreadable_rows_yield_no_windows_and_never_raise(body):
    assert usage_probe.anthropic_model_windows(body) == ()


def test_a_bad_reset_time_keeps_the_percentage_and_duplicates_are_dropped():
    body = {"limits": [{**FABLE_ROW, "resets_at": "not a date"}, {**FABLE_ROW, "percent": 99}]}
    (window,) = usage_probe.anthropic_model_windows(body)
    assert window.percent == 26.0 and window.resets_at is None


def test_the_anthropic_read_carries_model_windows_beside_its_headers(monkeypatch):
    monkeypatch.setattr(usage_probe, "_get", lambda url, headers: (200, None, {
        "five_hour": {"utilization": 3.0, "resets_at": "2026-09-16T15:00:00+00:00"},
        "seven_day": {"utilization": 23.0, "resets_at": "2026-09-22T14:00:00+00:00"},
        "limits": [FABLE_ROW]}))
    result = usage_probe.fetch_anthropic_usage("tok")
    assert result.headers and [w.name for w in result.model_windows] == ["Fable"]


# ---- the router keeps them across real traffic ------------------------------

def _rt(**kw):
    return ProfileRuntime(profile_id="a", priority=1, switch_threshold=98, automatic=True, **kw)


def test_a_real_response_never_erases_the_windows_a_usage_read_found():
    fable = ModelWindow(name="Fable", percent=26, resets_at=None)
    pool = PoolSnapshot(profiles=[_rt(model_usage=(fable,))])
    real = UsageSnapshot(percent=40, resets_at=None, confidence="measured")   # model_windows None
    (after,) = observe(pool, "a", real, NOW).profiles
    assert after.model_usage == (fable,) and after.last_usage_percent == 40


def test_a_usage_read_replaces_the_windows_even_with_none_left():
    pool = PoolSnapshot(profiles=[_rt(model_usage=(ModelWindow("Fable", 26, None),))])
    read = UsageSnapshot(percent=40, resets_at=None, confidence="measured", model_windows=())
    (after,) = observe(pool, "a", read, NOW).profiles
    assert after.model_usage == ()


def test_a_spent_model_window_does_not_change_the_accounts_state():
    # Display-only: 100% Fable on an account at 10% overall stays ELIGIBLE.
    pool = PoolSnapshot(profiles=[_rt()])
    read = UsageSnapshot(percent=10, resets_at=None, confidence="measured",
                         model_windows=(ModelWindow("Fable", 100, None),))
    (after,) = observe(pool, "a", read, NOW).profiles
    assert after.state.value == "eligible"


# ---- the daemon path and persistence ----------------------------------------

@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    return tmp_path


def _probe_headers(pct=0.03):
    return {"anthropic-ratelimit-unified-5h-utilization": str(pct),
            "anthropic-ratelimit-unified-5h-reset": str(int(time_module.time()) + 3600)}


def test_record_ping_attaches_the_windows_and_they_survive_a_restart(pool_env, monkeypatch):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: None)
    monkeypatch.setattr(daemon, "_gateway", gw)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    gw.runtime_snapshot()   # the Profile exists in the runtime before the read lands

    daemon._record_ping(Profile(id="a", name="A", kind="oauth"),
                        {"status": 200, "headers": _probe_headers()},
                        model_windows=(ModelWindow("Fable", 26.0, future, True),
                                       ModelWindow("Opus", 90.0, past)))
    assert [w.name for w in gw.runtime_snapshot()["a"].model_usage] == ["Fable", "Opus"]

    restored = Gateway(transport=lambda req: None).runtime_snapshot()["a"]
    # The window past its own reset is not restored; the live one is, intact.
    assert restored.model_usage == (ModelWindow("Fable", 26.0, future, True),)


def test_public_dict_exposes_model_usage_for_every_kind():
    fable = ModelWindow("Fable", 26.0, datetime(2026, 9, 22, tzinfo=timezone.utc), True)
    for kind in ("oauth", "codex", "api"):
        p = Profile(id="a", name="A", kind=kind)
        out = daemon._profile_to_public_dict(p, _rt(model_usage=(fable,) if kind == "oauth" else ()))
        expected = [{"name": "Fable", "percent": 26.0, "resets_at": "2026-09-22T00:00:00+00:00",
                     "active": True}] if kind == "oauth" else []
        assert out["model_usage"] == expected
    assert daemon._profile_to_public_dict(Profile(id="a", name="A", kind="oauth"))["model_usage"] == []


def test_the_per_tick_runtime_rebuild_keeps_the_windows(pool_env):
    # _sync_snapshot rebuilds every ProfileRuntime field by field on every
    # Dashboard poll; a field it forgets vanishes about a second after it lands.
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: None)
    gw.runtime_snapshot()
    with gw._lock:
        gw._observe("a", UsageSnapshot(percent=3, resets_at=None, confidence="measured",
                                       model_windows=(ModelWindow("Fable", 26.0, None),)), NOW)
    for _ in range(3):
        assert [w.name for w in gw.runtime_snapshot()["a"].model_usage] == ["Fable"]


def test_a_naive_persisted_reset_time_never_breaks_startup(pool_env):
    # Compared with an aware `now` at startup; a naive value used to be a
    # TypeError there. Treated as UTC, which is what we always write.
    from claude_unlimited.gateway import _restorable_usage_fields
    future_naive = (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None).isoformat()
    fields = _restorable_usage_fields(
        {"model_usage": [{"name": "Fable", "percent": 26, "resets_at": future_naive}]},
        now=datetime.now(timezone.utc))
    (window,) = fields["model_usage"]
    assert window.resets_at.tzinfo is not None


def test_last_use_is_the_latest_by_time_not_by_insertion_order():
    from claude_unlimited.usage_history import UsageEvent, last_use_by_profile
    def ev(ts, model):
        return UsageEvent(timestamp=ts, profile_id="a", project_id=None, model=model, input_tokens=1,
                          output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, cost_usd=None)
    newer, older = ev("2026-09-17T10:00:00+00:00", "claude-fable-5-1"), ev("2026-09-10T10:00:00+00:00", "claude-haiku-4-5")
    assert last_use_by_profile([newer, older])["a"]["model"] == "claude-fable-5-1"   # an import appended the old row last
