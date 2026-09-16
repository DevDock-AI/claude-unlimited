"""Background usage checks: response parsing, scheduling, backoff, and the
daemon tick that feeds readings into the Dashboard's numbers.

Fully offline — conftest refuses any real usage-endpoint call, and every test
that exercises the HTTP layer installs its own fake.
"""

import email.message
import json
import time
import urllib.error
from datetime import datetime, timezone

import pytest

import claude_unlimited.activity as activity
import claude_unlimited.observation as observation
import claude_unlimited.openai_observation as openai_observation
import claude_unlimited.usage_probe as usage_probe
from claude_unlimited.usage_probe import Candidate, ProbeResult, Scheduler

# Response shapes as the real endpoints returned them (2026-09-15), with every
# identifying field removed.
ANTHROPIC_BODY = {
    "five_hour": {"utilization": 46.0, "resets_at": "2026-09-16T00:00:00.908684+00:00"},
    "seven_day": {"utilization": 10.0, "resets_at": "2026-09-22T14:00:00.908705+00:00"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False},
}
CODEX_BODY = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {"used_percent": 4, "limit_window_seconds": 18000,
                           "reset_after_seconds": 11813, "reset_at": 1789510854},
        "secondary_window": {"used_percent": 38, "limit_window_seconds": 604800,
                             "reset_after_seconds": 328174, "reset_at": 1789827214},
    },
}
NOW = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)


# ---- parsing into the existing classifiers ------------------------------------

def test_anthropic_usage_becomes_the_same_snapshot_a_real_response_gives():
    headers = usage_probe.anthropic_usage_headers(ANTHROPIC_BODY)
    snap = observation.classify(200, headers, NOW)
    assert isinstance(snap, observation.UsageSnapshot)
    assert snap.percent == 46.0 and snap.percent_7d == 10.0  # endpoint is 0-100, headers 0-1
    assert snap.resets_at == datetime(2026, 9, 16, 0, 0, 0, tzinfo=timezone.utc)


def test_codex_usage_becomes_the_same_snapshot_a_real_response_gives():
    headers = usage_probe.codex_usage_headers(CODEX_BODY)
    snap = openai_observation.classify(200, headers, NOW)
    assert isinstance(snap, observation.UsageSnapshot)
    assert snap.percent == 4 and snap.percent_7d == 38
    assert headers["x-codex-plan-type"] == "plus"


@pytest.mark.parametrize("body", [None, [], {}, {"five_hour": None}, {"five_hour": {"utilization": "46"}},
                                  {"seven_day": {"utilization": 10.0}}])
def test_unreadable_anthropic_shapes_yield_nothing(body):
    assert usage_probe.anthropic_usage_headers(body) is None


@pytest.mark.parametrize("body", [None, {}, {"rate_limit": None}, {"rate_limit": {"primary_window": None}}])
def test_unreadable_codex_shapes_yield_nothing(body):
    assert usage_probe.codex_usage_headers(body) is None


# ---- HTTP layer (fake transport) ----------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_anthropic_read_is_a_get_with_the_oauth_usage_headers(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(),
                    headers={k.lower(): v for k, v in request.header_items()})
        return FakeResponse(ANTHROPIC_BODY)

    monkeypatch.setattr(usage_probe, "_urlopen", fake)
    result = usage_probe.fetch_anthropic_usage("tok-abc")
    assert seen["url"] == usage_probe.ANTHROPIC_USAGE_URL and seen["method"] == "GET"
    assert seen["headers"]["authorization"] == "Bearer tok-abc"
    assert seen["headers"]["anthropic-beta"] == usage_probe.ANTHROPIC_OAUTH_BETA
    assert result.status == 200 and result.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.46"


def test_a_rate_limit_carries_retry_after_and_a_dead_network_has_no_status(monkeypatch):
    hdrs = email.message.Message()
    hdrs["Retry-After"] = "120"

    def limited(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", hdrs, None)

    monkeypatch.setattr(usage_probe, "_urlopen", limited)
    assert usage_probe.fetch_anthropic_usage("t") == ProbeResult(status=429, retry_after=120.0)

    def offline(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(usage_probe, "_urlopen", offline)
    assert usage_probe.fetch_anthropic_usage("t").status is None


def test_only_subscription_profiles_have_a_usage_endpoint():
    class P:
        def __init__(self, kind, auth_mode="api_key"):
            self.kind, self.auth_mode = kind, auth_mode
    assert usage_probe.provider_for(P("oauth")) == "anthropic"
    assert usage_probe.provider_for(P("codex", "chatgpt_subscription")) == "openai"
    assert usage_probe.provider_for(P("codex", "api_key")) is None
    assert usage_probe.provider_for(P("api")) is None


# ---- scheduling -----------------------------------------------------------------

@pytest.fixture
def sched(tmp_path):
    clock = [1_800_000_000.0]
    s = Scheduler(clock=lambda: clock[0], state_file=tmp_path / "state.json")
    s.clock = clock
    return s


def advance(s, seconds, active=True):
    s.clock[0] += seconds
    if active:
        s.note_activity()


def test_nothing_is_read_until_someone_is_active(sched):
    candidate = [Candidate("a", "anthropic", None)]
    assert sched.due(candidate) == []
    assert sched.note_activity() is True          # the first sign of life ends "idle"
    assert sched.due(candidate) == ["a"]
    assert sched.note_activity() is False


def test_reads_stop_after_thirty_idle_minutes_and_resume_on_activity(sched):
    candidate = [Candidate("a", "anthropic", None)]
    sched.note_activity()
    advance(sched, usage_probe.IDLE_AFTER_SECONDS + 1, active=False)
    assert sched.due(candidate) == []
    assert sched.note_activity() is True           # coming back is reported, so the caller checks at once
    assert sched.due(candidate) == ["a"]


def test_an_account_is_read_every_five_to_ten_minutes_from_its_last_reading(sched):
    sched.note_activity()
    now = sched.clock[0]
    assert sched.due([Candidate("a", "anthropic", now - 299)]) == []    # refreshed recently (real traffic or a read)
    assert sched.due([Candidate("a", "anthropic", now - 601)]) == ["a"]
    for pid in ("a", "b", "c", "d", "e", "f"):
        assert 300 <= usage_probe.interval_for(pid, now) <= 600


def test_at_most_two_reads_per_tick_oldest_first(sched):
    sched.note_activity()
    now = sched.clock[0]
    picked = sched.due([Candidate("fresh-ish", "anthropic", now - 700),
                        Candidate("oldest", "anthropic", now - 5000),
                        Candidate("never", "openai", None)])
    assert picked == ["never", "oldest"]


def test_a_rate_limit_honours_retry_after_and_rests_the_whole_provider(sched):
    sched.note_activity()
    everyone = [Candidate("a", "anthropic", None), Candidate("b", "anthropic", None),
                Candidate("c", "openai", None)]
    msg = sched.record("a", "anthropic", ProbeResult(status=429, retry_after=3600))
    assert "rate limited" in msg and "60 min" in msg   # Retry-After wins over the 15 min floor
    assert sched.due(everyone) == ["c"]            # the other Anthropic account waits too
    advance(sched, 3599)
    assert "a" not in sched.due(everyone)
    advance(sched, 2)
    assert set(sched.due(everyone)) >= {"a"}


def test_repeated_rate_limits_escalate_to_a_ceiling(sched):
    waits = []
    for _ in range(8):
        sched.record("a", "anthropic", ProbeResult(status=429))
        waits.append(sched._loaded()["profiles"]["a"]["not_before"] - sched.clock[0])
    assert waits[:3] == [900, 1800, 3600]
    assert max(waits) == usage_probe.RATE_LIMIT_BACKOFF_CEILING_SECONDS


def test_a_refused_credential_backs_off_for_hours_without_resting_the_provider(sched):
    sched.note_activity()
    assert "HTTP 403" in sched.record("a", "anthropic", ProbeResult(status=403))
    assert sched.due([Candidate("a", "anthropic", None), Candidate("b", "anthropic", None)]) == ["b"]
    assert sched.record("a", "anthropic", ProbeResult(status=403)) is None   # said once, not every time
    assert sched._loaded()["profiles"]["a"]["not_before"] - sched.clock[0] == 7200


def test_a_success_clears_the_backoff(sched):
    sched.record("a", "anthropic", ProbeResult(status=500))
    sched.record("a", "anthropic", ProbeResult(status=200, headers={"x": "1"}))
    assert "a" not in sched._loaded()["profiles"]


def test_an_unreadable_200_counts_as_a_failure_and_is_logged_on_the_third(sched):
    messages = [sched.record("a", "anthropic", ProbeResult(status=200, headers=None)) for _ in range(4)]
    assert messages[0] is None and messages[1] is None and messages[3] is None
    assert "3 times in a row" in messages[2]


def test_backoff_survives_a_restart(sched, tmp_path):
    sched.record("a", "anthropic", ProbeResult(status=429))
    reborn = Scheduler(clock=lambda: sched.clock[0], state_file=tmp_path / "state.json")
    reborn.note_activity()
    assert reborn.due([Candidate("a", "anthropic", None), Candidate("b", "anthropic", None)]) == []


# ---- the daemon tick ------------------------------------------------------------

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
def daemon_env(monkeypatch, tmp_path):
    import claude_unlimited.daemon as daemon
    import claude_unlimited.gateway as gateway_module
    import claude_unlimited.profiles as profile_repo

    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(gateway_module, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    gw = gateway_module.Gateway(transport=lambda req: None)
    monkeypatch.setattr(daemon, "_gateway", gw)
    scheduler = Scheduler(state_file=tmp_path / "usage_probe_state.json")
    monkeypatch.setattr(daemon, "_usage_probe", scheduler)
    return daemon, gw, scheduler, profile_repo


def test_a_tick_reads_usage_into_the_dashboard_numbers_only_while_active(daemon_env, monkeypatch):
    daemon, gw, scheduler, profile_repo = daemon_env
    p = profile_repo.create_profile(name="Max", kind="oauth", credential="sk-ant-12345678", account_uuid="u1")
    calls = []
    monkeypatch.setattr(usage_probe, "fetch_anthropic_usage", lambda token: calls.append(token) or
                        ProbeResult(status=200, headers=usage_probe.anthropic_usage_headers(ANTHROPIC_BODY)))

    daemon._run_usage_probe_tick()
    assert calls == []                              # idle: nothing sent

    scheduler.note_activity()
    daemon._run_usage_probe_tick()
    assert calls == ["sk-ant-12345678"]
    assert gw.runtime_snapshot()[p.id].last_usage_percent == 46.0

    daemon._run_usage_probe_tick()
    assert len(calls) == 1                          # just read: not due again for 5-10 min


def test_a_tick_sends_nothing_when_the_setting_is_off_or_for_api_key_profiles(daemon_env, monkeypatch):
    daemon, _gw, scheduler, profile_repo = daemon_env
    from claude_unlimited.config import update_settings

    profile_repo.create_profile(name="Console", kind="api", credential="sk-ant-api-12345678")
    oauth = profile_repo.create_profile(name="Max", kind="oauth", credential="sk-ant-12345678", account_uuid="u1")
    calls = []
    monkeypatch.setattr(usage_probe, "fetch_anthropic_usage", lambda token: calls.append(token) or ProbeResult(status=500))
    scheduler.note_activity()

    update_settings(keep_usage_fresh=False)
    daemon._run_usage_probe_tick()
    assert calls == []

    update_settings(keep_usage_fresh=True)
    daemon._run_usage_probe_tick()
    assert calls == ["sk-ant-12345678"]             # the oauth one only; never the API-key Profile
    assert profile_repo.list_profiles()[1].id == oauth.id
