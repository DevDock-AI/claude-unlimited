"""The rate-limit refresh backoff must survive a daemon restart, the same way
the needs-re-auth state already does. Without it, a rate-limited OAuth account
re-pokes Anthropic's token endpoint the instant the daemon comes back — and
frequent restarts (auto-update, a crash loop) keep it rate-limited, the way
repeated manual restarts did. See Gateway._restore_refresh_backoff and _persist.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, load_pool, save_pool
from claude_unlimited.gateway import Gateway


class _FakeSecretStore:
    def get_token(self, profile_id):
        return "tok-" + profile_id


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE",
                        tmp_path / "runtime_state.json")
    monkeypatch.setattr(gateway_module, "secret_store", _FakeSecretStore())
    # Isolate these tests from the refresh machinery: we exercise backoff
    # persistence, not the preventive token check that would otherwise fire the
    # first time _sync_snapshot sees an ELIGIBLE Profile.
    monkeypatch.setattr(Gateway, "_maybe_check_oauth_credential", lambda self, p, rt: None)
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    return tmp_path


def _new_gateway():
    """A Gateway that has loaded runtime_state.json (in __init__) and folded the
    Pool in — i.e. exactly the state right after a daemon restart."""
    gw = Gateway(transport=lambda req: None)
    gw._sync_snapshot(load_pool())
    return gw


def test_active_backoff_persists_and_blocks_repoke_after_restart(env):
    gw = _new_gateway()
    # Stand in for "we took several 429s and backed off an hour."
    gw._refresh_rate_limited_streak["a"] = 3
    gw._refresh_check_not_before["a"] = time.monotonic() + 3600
    gw._persist()

    restarted = _new_gateway()
    assert restarted._refresh_rate_limited_streak.get("a") == 3
    # The whole point: it will NOT immediately re-poke the token endpoint.
    assert restarted._refresh_attempt_due("a") is False


def test_elapsed_backoff_keeps_streak_but_allows_a_fresh_attempt(env):
    gw = _new_gateway()
    gw._refresh_rate_limited_streak["a"] = 2
    gw._refresh_check_not_before["a"] = time.monotonic() + 3600
    gw._persist()

    # Simulate the daemon having been down long enough for the deadline to pass.
    state_file = env / "runtime_state.json"
    data = json.loads(state_file.read_text())
    data["profiles"]["a"]["refresh_backoff_until"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    state_file.write_text(json.dumps(data))

    restarted = _new_gateway()
    # Streak is kept so the NEXT 429 keeps escalating instead of restarting.
    assert restarted._refresh_rate_limited_streak.get("a") == 2
    # But the backoff window itself has elapsed, so an attempt is due.
    assert restarted._refresh_attempt_due("a") is True


def test_plain_refresh_throttle_is_not_persisted(env):
    gw = _new_gateway()
    # No rate-limit streak — just the ordinary 60s throttle every refresh sets.
    gw._refresh_check_not_before["a"] = time.monotonic() + 60
    gw._persist()

    data = json.loads((env / "runtime_state.json").read_text())
    assert data["profiles"]["a"]["refresh_rate_limited_streak"] is None
    assert data["profiles"]["a"]["refresh_backoff_until"] is None
    # A restart must not invent a backoff out of the plain throttle.
    assert _new_gateway()._refresh_attempt_due("a") is True


def test_persisted_deadline_is_wall_clock_not_monotonic(env):
    """The stored deadline has to be a real timestamp; a raw monotonic float is
    meaningless in the next process and would restore nonsense."""
    gw = _new_gateway()
    gw._refresh_rate_limited_streak["a"] = 1
    gw._refresh_check_not_before["a"] = time.monotonic() + 1800
    gw._persist()

    raw = json.loads((env / "runtime_state.json").read_text())["profiles"]["a"]["refresh_backoff_until"]
    remaining = (datetime.fromisoformat(raw) - datetime.now(timezone.utc)).total_seconds()
    assert 1500 < remaining <= 1800
