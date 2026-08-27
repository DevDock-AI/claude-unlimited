import threading
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.usage_history as usage_history


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    return tmp_path


def test_list_events_empty_by_default(env):
    assert usage_history.list_events() == []


def test_record_persists_and_computes_cost(env):
    event = usage_history.record("prof-a", "-Users-a-app", "claude-sonnet-5",
                                  {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert event.cost_usd == pytest.approx(2.0 + 10.0)
    events = usage_history.list_events()
    assert len(events) == 1
    assert events[0].profile_id == "prof-a"
    assert events[0].project_id == "-Users-a-app"


def test_record_unknown_model_has_none_cost(env):
    event = usage_history.record("prof-a", None, "totally-unknown-model", {"input_tokens": 10, "output_tokens": 10})
    assert event.cost_usd is None


def test_record_missing_usage_fields_default_to_zero(env):
    event = usage_history.record("prof-a", None, "claude-sonnet-5", {})
    assert event.input_tokens == 0
    assert event.output_tokens == 0


def test_list_events_survives_corrupt_line(env):
    usage_history.USAGE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    usage_history.record("prof-a", None, "claude-sonnet-5", {"input_tokens": 1, "output_tokens": 1})
    with usage_history.USAGE_HISTORY_FILE.open("a") as f:
        f.write("not valid json\n")
    events = usage_history.list_events()
    assert len(events) == 1


def test_trims_to_max_events(env, monkeypatch):
    monkeypatch.setattr(usage_history, "MAX_EVENTS", 5)
    for i in range(10):
        usage_history.record(f"prof-{i}", None, "claude-sonnet-5", {"input_tokens": 1, "output_tokens": 1})
    events = usage_history.list_events()
    assert len(events) == 5
    assert events[-1].profile_id == "prof-9"  # newest survives


def test_reset_clears_history(env):
    usage_history.record("prof-a", None, "claude-sonnet-5", {"input_tokens": 1, "output_tokens": 1})
    usage_history.reset()
    assert usage_history.list_events() == []


def test_record_is_thread_safe_under_concurrent_calls(env):
    threads_count, calls_per_thread = 15, 20

    def hammer():
        for _ in range(calls_per_thread):
            usage_history.record("prof-a", None, "claude-sonnet-5", {"input_tokens": 1, "output_tokens": 1})

    threads = [threading.Thread(target=hammer) for _ in range(threads_count)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(usage_history.list_events()) == threads_count * calls_per_thread


# ---- aggregation helpers (pure, no I/O) ----

def _event_at(dt: datetime, **kwargs) -> usage_history.UsageEvent:
    defaults = dict(profile_id="p", project_id=None, model="claude-sonnet-5",
                     input_tokens=100, output_tokens=100, cache_creation_input_tokens=0,
                     cache_read_input_tokens=0, cost_usd=0.01)
    defaults.update(kwargs)
    return usage_history.UsageEvent(timestamp=dt.isoformat(), **defaults)


def test_daily_totals_buckets_by_local_calendar_day():
    now_local = datetime.now().astimezone()
    today_utc = now_local.astimezone(timezone.utc)
    yesterday_utc = (now_local - timedelta(days=1)).astimezone(timezone.utc)
    events = [
        _event_at(today_utc, input_tokens=100, output_tokens=50),
        _event_at(yesterday_utc, input_tokens=10, output_tokens=10),
    ]
    totals = usage_history.daily_totals(events, days=7)
    assert len(totals) == 7
    assert totals[-1]["tokens"] == 150  # today, last in the oldest-first list
    assert totals[-2]["tokens"] == 20  # yesterday


def test_daily_totals_ignores_events_outside_window():
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    totals = usage_history.daily_totals([_event_at(long_ago, input_tokens=999, output_tokens=999)], days=7)
    assert sum(t["tokens"] for t in totals) == 0


def test_daily_totals_by_profile_splits_the_same_day_bucket_per_profile():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now, profile_id="a", input_tokens=100, output_tokens=0),
        _event_at(now, profile_id="a", input_tokens=50, output_tokens=0),
        _event_at(now, profile_id="b", input_tokens=30, output_tokens=0),
    ]
    totals = usage_history.daily_totals_by_profile(events, days=7)
    assert len(totals) == 7
    today = totals[-1]
    assert today["profiles"] == {"a": 150, "b": 30}


def test_daily_totals_by_profile_omits_profiles_with_no_tokens_that_day():
    now = datetime.now(timezone.utc)
    totals = usage_history.daily_totals_by_profile([_event_at(now, profile_id="a", input_tokens=10, output_tokens=0)], days=7)
    assert "b" not in totals[-1]["profiles"]


def test_daily_totals_by_profile_ignores_events_outside_window():
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    totals = usage_history.daily_totals_by_profile([_event_at(long_ago, profile_id="a", input_tokens=999, output_tokens=999)], days=7)
    assert all(not d["profiles"] for d in totals)


def test_model_split_computes_percentages():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now, model="claude-sonnet-5", input_tokens=300, output_tokens=0),
        _event_at(now, model="claude-haiku-4-5", input_tokens=100, output_tokens=0),
    ]
    split = usage_history.model_split(events)
    assert split[0]["model"] == "claude-sonnet-5"
    assert split[0]["percent"] == 75.0
    assert split[1]["percent"] == 25.0


def test_model_split_empty_events_returns_empty():
    assert usage_history.model_split([]) == []


def test_model_split_ignores_events_with_no_model():
    events = [_event_at(datetime.now(timezone.utc), model=None)]
    assert usage_history.model_split(events) == []


def test_filter_events_since_keeps_only_events_within_range():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now - timedelta(minutes=30), input_tokens=1, output_tokens=0),  # within 1h
        _event_at(now - timedelta(hours=3), input_tokens=1, output_tokens=0),  # outside 1h, within 1d
        _event_at(now - timedelta(days=10), input_tokens=1, output_tokens=0),  # outside 1d and 1w
    ]
    assert len(usage_history.filter_events_since(events, "1h")) == 1
    assert len(usage_history.filter_events_since(events, "1d")) == 2
    assert len(usage_history.filter_events_since(events, "1w")) == 2
    assert len(usage_history.filter_events_since(events, "1y")) == 3


def test_filter_events_since_unrecognized_range_returns_all_events():
    events = [_event_at(datetime.now(timezone.utc) - timedelta(days=1000))]
    assert usage_history.filter_events_since(events, "not-a-real-range") == events


def test_range_granularity_covers_every_range_key():
    assert set(usage_history.RANGE_GRANULARITY.keys()) == set(usage_history.RANGE_KEYS)
    assert set(usage_history.RANGE_GRANULARITY.values()) <= {"hour", "day", "week", "month"}
    assert usage_history.RANGE_GRANULARITY["1d"] == "hour"  # last 24 hours, not since-midnight


def test_range_to_days_covers_only_day_granularity_ranges():
    day_ranges = {k for k, v in usage_history.RANGE_GRANULARITY.items() if v == "day"}
    assert set(usage_history.RANGE_TO_DAYS.keys()) == day_ranges
    assert all(1 <= d <= 31 for d in usage_history.RANGE_TO_DAYS.values())


def test_hourly_totals_buckets_by_rolling_1_hour_windows():
    now_local = datetime.now().astimezone()
    events = [
        _event_at(now_local.astimezone(timezone.utc), input_tokens=100, output_tokens=0),  # this hour
        _event_at((now_local - timedelta(hours=5)).astimezone(timezone.utc), input_tokens=50, output_tokens=0),  # 5h ago
        _event_at((now_local - timedelta(hours=30)).astimezone(timezone.utc), input_tokens=999, output_tokens=0),  # outside 24h window
    ]
    totals = usage_history.hourly_totals(events, hours=24)
    assert len(totals) == 24
    assert totals[-1]["tokens"] == 100  # most recent bucket, last in oldest-first list
    assert totals[-6]["tokens"] == 50  # bucket covering 5 hours ago
    assert sum(t["tokens"] for t in totals) == 150  # the 30-hours-ago event is outside every bucket


def test_hourly_totals_covers_real_last_24h_not_since_local_midnight():
    # daily_totals(days=1) only covers today since local midnight, dropping
    # events from earlier in the 24h window when "now" is e.g. 2am.
    # hourly_totals must not have that gap.
    now_local = datetime.now().astimezone()
    just_before_midnight_23h_ago = now_local - timedelta(hours=23, minutes=30)
    events = [_event_at(just_before_midnight_23h_ago.astimezone(timezone.utc), input_tokens=42, output_tokens=0)]
    totals = usage_history.hourly_totals(events, hours=24)
    assert sum(t["tokens"] for t in totals) == 42


def test_hourly_totals_with_6_hour_buckets_gives_4_bars_over_24h():
    # 24 one-hour bars are too dense for the chart card's width, so the
    # Dashboard requests bucket_hours=6 for the "1d" range.
    now_local = datetime.now().astimezone()
    events = [
        _event_at(now_local.astimezone(timezone.utc), input_tokens=100, output_tokens=0),  # most recent bucket
        _event_at((now_local - timedelta(hours=20)).astimezone(timezone.utc), input_tokens=50, output_tokens=0),  # oldest bucket
        _event_at((now_local - timedelta(hours=30)).astimezone(timezone.utc), input_tokens=999, output_tokens=0),  # outside 24h window
    ]
    totals = usage_history.hourly_totals(events, hours=24, bucket_hours=6)
    assert len(totals) == 4
    assert totals[-1]["tokens"] == 100
    assert totals[0]["tokens"] == 50
    assert sum(t["tokens"] for t in totals) == 150


def test_weekly_totals_buckets_by_rolling_7_day_windows():
    now_local = datetime.now().astimezone()
    events = [
        _event_at(now_local.astimezone(timezone.utc), input_tokens=100, output_tokens=0),  # this week
        _event_at((now_local - timedelta(days=10)).astimezone(timezone.utc), input_tokens=50, output_tokens=0),  # 2 weeks ago
        _event_at((now_local - timedelta(days=40)).astimezone(timezone.utc), input_tokens=999, output_tokens=0),  # outside 5-week window
    ]
    totals = usage_history.weekly_totals(events, weeks=5)
    assert len(totals) == 5
    assert totals[-1]["tokens"] == 100  # most recent bucket, last in oldest-first list
    assert totals[-2]["tokens"] == 50  # bucket covering 10 days ago
    assert sum(t["tokens"] for t in totals) == 150  # the 40-days-ago event fell outside every bucket


def test_monthly_totals_buckets_by_real_calendar_month():
    today = datetime.now().astimezone().date()
    this_month_start = today.replace(day=1)
    # A definitely-different month: 40 days before this month's 1st.
    other_month_day = this_month_start - timedelta(days=40)
    events = [
        _event_at(datetime.now(timezone.utc), input_tokens=100, output_tokens=0),
        _event_at(datetime(other_month_day.year, other_month_day.month, other_month_day.day, tzinfo=timezone.utc),
                   input_tokens=50, output_tokens=0),
    ]
    totals = usage_history.monthly_totals(events, months=12)
    assert len(totals) == 12
    assert totals[-1]["date"] == this_month_start.isoformat()
    assert totals[-1]["tokens"] == 100
    assert sum(t["tokens"] for t in totals) == 150  # both events land inside a 12-month window


def test_monthly_totals_empty_events_returns_zeroed_buckets():
    totals = usage_history.monthly_totals([], months=12)
    assert len(totals) == 12
    assert all(t["tokens"] == 0 for t in totals)


def test_hourly_histogram_has_24_buckets_and_counts_local_hour():
    now_local = datetime.now().astimezone()
    events = [_event_at(now_local.astimezone(timezone.utc))]
    hist = usage_history.hourly_histogram(events)
    assert len(hist) == 24
    assert hist[now_local.hour] == 1
    assert sum(hist) == 1


def test_cost_by_profile_sums_and_skips_unknown_models():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now, profile_id="a", cost_usd=0.5),
        _event_at(now, profile_id="a", cost_usd=0.25),
        _event_at(now, profile_id="b", cost_usd=1.0),
        _event_at(now, profile_id="c", cost_usd=None),
    ]
    totals = usage_history.cost_by_profile(events)
    assert totals == {"a": 0.75, "b": 1.0}


def test_usage_by_profile_sums_tokens_always_and_cost_when_known():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now, profile_id="a", input_tokens=100, output_tokens=50, cost_usd=0.5),
        _event_at(now, profile_id="a", input_tokens=10, output_tokens=10, cost_usd=0.25),
        _event_at(now, profile_id="b", input_tokens=5, output_tokens=5, cost_usd=None),  # unpriced model
    ]
    totals = usage_history.usage_by_profile(events)
    assert totals["a"]["tokens"] == 170
    assert totals["a"]["cost_usd"] == pytest.approx(0.75)
    assert totals["b"]["tokens"] == 10  # tokens still counted even with no price
    assert totals["b"]["cost_usd"] is None  # None (not 0), since no priced event ever happened


def test_tokens_by_project_sums_tokens_and_cost():
    now = datetime.now(timezone.utc)
    events = [
        _event_at(now, project_id="-Users-a-app", input_tokens=100, output_tokens=50, cost_usd=0.1),
        _event_at(now, project_id="-Users-a-app", input_tokens=10, output_tokens=10, cost_usd=0.05),
        _event_at(now, project_id="-Users-b-other", input_tokens=5, output_tokens=5, cost_usd=0.01),
    ]
    totals = usage_history.tokens_by_project(events)
    assert totals["-Users-a-app"]["tokens"] == 170
    assert totals["-Users-a-app"]["cost_usd"] == pytest.approx(0.15)
    assert totals["-Users-b-other"]["tokens"] == 10


def test_tokens_by_project_ignores_events_without_project():
    now = datetime.now(timezone.utc)
    events = [_event_at(now, project_id=None)]
    assert usage_history.tokens_by_project(events) == {}


def test_tokens_by_project_cost_none_when_no_priced_events():
    now = datetime.now(timezone.utc)
    events = [_event_at(now, project_id="-Users-a-app", cost_usd=None)]
    totals = usage_history.tokens_by_project(events)
    assert totals["-Users-a-app"]["cost_usd"] is None


def test_a_line_with_the_wrong_shape_is_skipped_not_fatal(monkeypatch, tmp_path):
    """This file feeds GET /api/profiles, both usage pages, and the api-kind
    token-budget check on the request path. One valid-JSON-wrong-shape line
    used to take all of them down together with a TypeError."""
    log = tmp_path / "usage_history.jsonl"
    monkeypatch.setattr(usage_history, "APP_DIR", tmp_path)
    monkeypatch.setattr(usage_history, "USAGE_HISTORY_FILE", log)

    usage_history.record("a", None, "claude-opus-4",
                         {"input_tokens": 10, "output_tokens": 20})
    with log.open("a", encoding="utf-8") as f:
        f.write('{"timestamp":"2026-01-02T00:00:00+00:00","profile_id":"a",'
                '"project_id":null,"model":"m","input_tokens":1,"output_tokens":1,'
                '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
                '"cost_usd":null,"reasoning_tokens":7}\n')   # a field this build does not know
        f.write('{"timestamp":"2026-01-02T00:00:00+00:00"}\n')  # missing fields
        f.write('[1, 2, 3]\n')                                   # not an object

    events = usage_history.list_events()
    assert [e.profile_id for e in events] == ["a"]
    assert events[0].input_tokens == 10
