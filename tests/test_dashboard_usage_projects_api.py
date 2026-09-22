import dataclasses
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo
import claude_unlimited.project_usage as project_usage
import claude_unlimited.usage_history as usage_history


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
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(project_usage, "USAGE_FILE", tmp_path / "project_usage.json")
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _request(url):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_empty_by_default(running_server):
    status, body = _request(f"{running_server}/api/usage/projects")
    assert status == 200
    assert body == {"projects": [], "total_requests": 0}


def test_reports_counts_and_percentages(running_server):
    project_usage.record_request("-Users-a-my-app")
    project_usage.record_request("-Users-a-my-app")
    project_usage.record_request("-Users-a-my-app")
    project_usage.record_request("-Users-b-other")

    status, body = _request(f"{running_server}/api/usage/projects")
    assert status == 200
    assert body["total_requests"] == 4
    top = body["projects"][0]
    assert top["project_id"] == "-Users-a-my-app"
    assert top["requests"] == 3
    assert top["percent"] == 75.0
    assert "display_name" in top


def test_reports_zero_tokens_when_no_usage_history(running_server):
    project_usage.record_request("-Users-a-my-app")
    status, body = _request(f"{running_server}/api/usage/projects")
    assert body["projects"][0]["tokens"] == 0
    assert body["projects"][0]["cost_usd"] is None


def test_merges_in_token_and_cost_totals_from_usage_history(running_server):
    project_usage.record_request("-Users-a-my-app")
    usage_history.record("prof-1", "-Users-a-my-app", "claude-sonnet-5",
                          {"input_tokens": 1_000_000, "output_tokens": 0})

    status, body = _request(f"{running_server}/api/usage/projects")
    top = body["projects"][0]
    assert top["tokens"] == 1_000_000
    assert top["cost_usd"] == pytest.approx(2.0)


def test_usage_summary_empty_by_default(running_server):
    status, body = _request(f"{running_server}/api/usage/summary")
    assert status == 200
    assert len(body["daily_totals"]) == 7
    assert body["model_split"] == []
    assert len(body["hourly_histogram"]) == 24
    assert body["cost_by_profile"] == {}
    assert body["total_events"] == 0
    assert "pricing_source" in body


def test_usage_summary_reflects_real_recorded_events(running_server):
    usage_history.record("prof-1", None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 100})
    usage_history.record("prof-1", None, "claude-haiku-4-5", {"input_tokens": 50, "output_tokens": 50})

    status, body = _request(f"{running_server}/api/usage/summary")
    assert body["total_events"] == 2
    assert sum(d["tokens"] for d in body["daily_totals"]) == 300
    assert len(body["model_split"]) == 2
    assert "prof-1" in body["cost_by_profile"]


def test_usage_summary_includes_per_account_daily_totals(running_server):
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    usage_history.record(p.id, None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 100})

    status, body = _request(f"{running_server}/api/usage/summary")
    assert status == 200
    assert len(body["daily_totals_by_profile"]) == 7
    today = body["daily_totals_by_profile"][-1]
    assert today["profiles"][p.id] == 200
    assert body["profile_names"][p.id] == "X"
    assert isinstance(body["profile_colors"], dict)


def test_usage_summary_per_account_totals_omit_ids_with_no_current_profile(running_server):
    # usage_history is append-only, so an id from a deleted Profile can still
    # appear in old events. Only ids that resolve to a currently-registered
    # Profile should reach the chart; a bare internal id means nothing to the
    # user.
    p = profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key")
    usage_history.record(p.id, None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 100})
    usage_history.record("d952512e251b2596", None, "claude-sonnet-5", {"input_tokens": 50, "output_tokens": 50})

    status, body = _request(f"{running_server}/api/usage/summary")
    assert status == 200
    today = body["daily_totals_by_profile"][-1]
    assert p.id in today["profiles"]
    assert "d952512e251b2596" not in today["profiles"]


def test_usage_summary_days_param_is_bounded(running_server):
    status, body = _request(f"{running_server}/api/usage/summary?days=999")
    assert len(body["daily_totals"]) == 31  # clamped, not 999
    status, body = _request(f"{running_server}/api/usage/summary?days=0")
    assert len(body["daily_totals"]) == 1  # clamped up from 0


def test_usage_summary_range_param_filters_by_real_elapsed_time(running_server):
    old_event = usage_history.UsageEvent(
        timestamp=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        profile_id="prof-1", project_id=None, model="claude-sonnet-5",
        input_tokens=500, output_tokens=0, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, cost_usd=1.0,
    )
    recent_event = usage_history.UsageEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        profile_id="prof-1", project_id=None, model="claude-sonnet-5",
        input_tokens=100, output_tokens=0, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, cost_usd=0.5,
    )
    import claude_unlimited.db as db
    for event in (old_event, recent_event):
        db.execute(
            """INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens,
                                        cache_creation_input_tokens, cache_read_input_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.timestamp, event.profile_id, event.project_id, event.model, event.input_tokens,
             event.output_tokens, event.cache_creation_input_tokens, event.cache_read_input_tokens,
             event.cost_usd))

    # Both events visible over a year
    status, body = _request(f"{running_server}/api/usage/summary?range=1y")
    assert body["model_split"][0]["tokens"] == 600

    # Only the recent one within the last hour
    status, body = _request(f"{running_server}/api/usage/summary?range=1h")
    assert body["model_split"][0]["tokens"] == 100
    assert body["range"] == "1h"


def test_usage_summary_unrecognized_range_falls_back_to_1w(running_server):
    status, body = _request(f"{running_server}/api/usage/summary?range=nonsense")
    assert body["range"] == "1w"
    assert len(body["daily_totals"]) == 7


def test_usage_summary_1w_is_day_granularity(running_server):
    status, body = _request(f"{running_server}/api/usage/summary?range=1w")
    assert body["granularity"] == "day"
    assert len(body["daily_totals"]) == 7


def test_usage_summary_1d_is_hour_granularity_last_24h_not_since_midnight(running_server):
    status, body = _request(f"{running_server}/api/usage/summary?range=1d")
    assert body["granularity"] == "hour"
    # 4 six-hour bars over the last 24h: 24 hour-bars are too dense for the
    # chart card's width, and since-local-midnight would drop earlier events.
    assert body["bucket_hours"] == 6
    assert len(body["daily_totals"]) == 4


def test_usage_summary_1m_is_week_granularity_not_30_day_bars(running_server):
    # 30 individual day-bars would blow out the chart's width, so 1m must be a
    # small, fixed number of weekly buckets.
    status, body = _request(f"{running_server}/api/usage/summary?range=1m")
    assert body["granularity"] == "week"
    assert len(body["daily_totals"]) == 5


def test_usage_summary_1y_is_month_granularity_not_365_day_bars(running_server):
    status, body = _request(f"{running_server}/api/usage/summary?range=1y")
    assert body["granularity"] == "month"
    assert len(body["daily_totals"]) == 12


def test_usage_stats_endpoint_returns_every_breakdown(running_server):
    """One endpoint applies one period filter uniformly, so the Stats view can
    never show a total that disagrees with its own breakdowns."""
    import claude_unlimited.db as db
    p = profile_repo.create_profile(name="Codex", kind="codex", credential="sk-codex-12345678")
    for model, requested, cost in [("gpt-5.6-sol", "claude-sonnet-5", 1.5),
                                   ("gpt-6-astra", "claude-fable-5-1", 2.0),
                                   ("gpt-5.6-sol", "claude-sonnet-5", 0.5)]:
        db.execute(
            """INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens,
                                        cache_creation_input_tokens, cache_read_input_tokens, cost_usd, requested_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), p.id, "-Users-a-app", model, 100, 20, 5, 300, cost, requested))

    status, body = _request(f"{running_server}/api/usage/stats?range=1m")

    assert status == 200
    assert body["totals"]["requests"] == 3
    assert body["totals"]["cost_usd"] == 4.0
    assert body["totals"]["cache_hit_percent"] is not None
    # sol is two rows totalling $2.00 and astra one row of $2.00; the tiebreak
    # is tokens, which sol wins with twice the volume.
    assert [r["key"] for r in body["by_model"]] == ["gpt-5.6-sol", "gpt-6-astra"]
    assert body["by_model"][0]["requests"] == 2 and body["by_model"][0]["cost_usd"] == 2.0
    assert sum(r["share"] for r in body["by_model"]) == 100.0
    assert body["by_profile"][0]["label"] == "Codex" and body["by_profile"][0]["kind"] == "codex"
    assert {r["key"] for r in body["by_requested_model"]} == {"claude-fable-5-1", "claude-sonnet-5"}
    assert body["history_begins"] is not None
    # A project is named, never shown as Claude Code's raw folder id — that is
    # a full local path on a screen people share.
    assert body["by_project"][0]["key"] == "-Users-a-app"
    assert body["by_project"][0]["label"] == "Users-a-app"   # no such folder here: best effort, no leading "-"


def test_usage_stats_rejects_a_bogus_range_instead_of_500ing(running_server):
    status, body = _request(f"{running_server}/api/usage/stats?range=all-of-time")
    assert status == 200 and body["range"] == "1m"
