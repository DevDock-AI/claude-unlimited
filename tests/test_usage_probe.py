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


# ---------------------------------------------------------------------------
# Codex per-model availability.
#
# OpenAI and Anthropic report different things and the bucket shape has to
# reflect that: Anthropic gives a percentage per model, OpenAI gives only a
# boolean. A Codex bucket is therefore binary — 0 available, 100 not — which
# still answers the routing question ("is this model blocked on this
# account"); the percentage is only how an Anthropic bucket answers it.
# ---------------------------------------------------------------------------

def test_codex_model_usage_becomes_binary_buckets():
    from claude_unlimited.usage_probe import codex_model_windows

    windows = codex_model_windows({"model_usage": {
        "gpt-6-astra": {"available": False, "available_at": 1790000000,
                        "credits_would_enable": True},
        "gpt-5.6-sol": {"available": True, "available_at": None},
    }})
    by_name = {w.name: w for w in windows}
    assert by_name["gpt-6-astra"].percent == 100.0
    assert by_name["gpt-6-astra"].resets_at is not None
    # The provider naming a model unavailable IS the binding limit here.
    assert by_name["gpt-6-astra"].active is True
    assert by_name["gpt-5.6-sol"].percent == 0.0
    assert by_name["gpt-5.6-sol"].resets_at is None
    assert by_name["gpt-5.6-sol"].active is False


def test_codex_model_usage_tolerates_the_shapes_the_endpoint_really_returns():
    from claude_unlimited.usage_probe import codex_model_windows

    # Verified live on a Plus account: an unconstrained model reports
    # available_at null. Nothing here may raise.
    assert codex_model_windows({}) == ()
    assert codex_model_windows({"model_usage": None}) == ()
    assert codex_model_windows({"model_usage": {}}) == ()
    assert codex_model_windows(None) == ()
    # Unknown availability is NOT the same as unavailable — skip it rather
    # than invent a block that would idle a working account.
    assert codex_model_windows({"model_usage": {"m": {"available": "yes"}}}) == ()
    assert codex_model_windows({"model_usage": {"m": {}}}) == ()
    assert codex_model_windows({"model_usage": {"": {"available": False}}}) == ()


def test_codex_available_at_accepts_epoch_or_iso():
    from claude_unlimited.usage_probe import codex_model_windows

    epoch = codex_model_windows({"model_usage": {
        "m": {"available": False, "available_at": 1790000000}}})[0]
    iso = codex_model_windows({"model_usage": {
        "m": {"available": False, "available_at": "2026-09-21T14:13:20Z"}}})[0]
    assert epoch.resets_at == iso.resets_at
    # A naive timestamp must not come back tz-unaware: gateway._blocked_models_for
    # and router.fable_spent compare it against an aware `now`, which would raise.
    naive = codex_model_windows({"model_usage": {
        "m": {"available": False, "available_at": "2026-09-21T14:13:20"}}})[0]
    assert naive.resets_at.tzinfo is not None
    junk = codex_model_windows({"model_usage": {
        "m": {"available": False, "available_at": "whenever"}}})[0]
    assert junk.resets_at is None


def test_a_codex_probe_carries_the_model_windows(monkeypatch):
    """The parser existing is not the point — fetch_codex_usage has to attach
    it, the way fetch_anthropic_usage does. That wiring is what was missing."""
    from claude_unlimited import usage_probe

    body = {"rate_limit": {"primary_window": {"used_percent": 10.0,
                                               "limit_window_seconds": 18000}},
            "model_usage": {"gpt-6-astra": {"available": False, "available_at": None}}}
    monkeypatch.setattr(usage_probe, "_get", lambda url, headers: (200, None, body))

    class Cred:
        access_token = "tok"
        account_id = "acct"

    result = usage_probe.fetch_codex_usage(Cred())
    assert result.status == 200
    assert result.model_windows and result.model_windows[0].name == "gpt-6-astra"
    assert result.model_windows[0].percent == 100.0


# ---------------------------------------------------------------------------
# The daemon side of the rejection-triggered re-read.
#
# A requested Profile is presented to the scheduler as "never observed", which
# skips ONLY the interval gate. The provider pause and the per-Profile backoff
# must still apply — the read is meant to happen sooner, not to bypass the
# protections that stop us hammering a provider that is already pushing back.
# ---------------------------------------------------------------------------

def test_a_requested_profile_skips_the_interval_but_not_the_backoff(tmp_path):
    from claude_unlimited import usage_probe

    clock = {"now": 10_000.0}
    sched = usage_probe.Scheduler(clock=lambda: clock["now"],
                                  state_file=tmp_path / "probe.json")
    sched.note_activity()

    just_read = clock["now"] - 5.0          # far inside the 5-10 min interval
    normal = usage_probe.Candidate("p1", usage_probe.PROVIDER_ANTHROPIC, just_read)
    # What _usage_probe_candidates() builds for a Profile that asked for a
    # re-read: the same candidate with observed_at=None.
    forced = usage_probe.Candidate("p1", usage_probe.PROVIDER_ANTHROPIC, None)

    assert sched.due([normal]) == []        # interval gate holds it back
    assert sched.due([forced]) == ["p1"]    # the request gets through

    # ...but a Profile inside its own backoff stays held back even when asked
    # for, which is the protection that must not be bypassed.
    sched.record("p1", usage_probe.PROVIDER_ANTHROPIC,
                 usage_probe.ProbeResult(status=429, retry_after=3600))
    assert sched.due([forced]) == []


def test_a_requested_profile_still_respects_the_provider_pause(tmp_path):
    from claude_unlimited import usage_probe

    clock = {"now": 20_000.0}
    sched = usage_probe.Scheduler(clock=lambda: clock["now"],
                                  state_file=tmp_path / "probe.json")
    sched.note_activity()
    forced = usage_probe.Candidate("p2", usage_probe.PROVIDER_ANTHROPIC, None)
    assert sched.due([forced]) == ["p2"]

    # A 429 pauses the whole provider; an out-of-band request must not poke it.
    sched.record("p2", usage_probe.PROVIDER_ANTHROPIC,
                 usage_probe.ProbeResult(status=429, retry_after=1800))
    assert sched.due([forced]) == []


def test_an_idle_pool_reads_nothing_even_when_asked(tmp_path):
    """No new polling (invariant 2): the re-read rides the existing tick, and
    that tick does not run while nobody is working."""
    from claude_unlimited import usage_probe

    clock = {"now": 30_000.0}
    sched = usage_probe.Scheduler(clock=lambda: clock["now"],
                                  state_file=tmp_path / "probe.json")
    # note_activity() never called -> never active.
    forced = usage_probe.Candidate("p3", usage_probe.PROVIDER_ANTHROPIC, None)
    assert sched.due([forced]) == []
