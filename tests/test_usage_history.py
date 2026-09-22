import threading
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.db as db
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


def test_history_is_kept_rather_than_trimmed(env, monkeypatch):
    """The old log discarded everything past MAX_EVENTS; a statistics view
    measured in months cannot be built on that."""
    monkeypatch.setattr(usage_history, "MAX_EVENTS", 5)
    for i in range(10):
        usage_history.record(f"prof-{i}", None, "claude-sonnet-5", {"input_tokens": 1, "output_tokens": 1})
    events = usage_history.list_events()
    assert len(events) == 10
    assert events[-1].profile_id == "prof-9" and events[0].profile_id == "prof-0"


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


def test_requested_model_is_recorded_when_the_served_model_differs(env):
    """A codex-kind Profile answers with the OpenAI model that actually ran, so
    the log alone could not tell a Fable request from an Opus one — both come
    back as a gpt-* id, only the mapping differs."""
    event = usage_history.record("prof-c", None, "gpt-6-astra",
                                 {"input_tokens": 10, "output_tokens": 5},
                                 requested_model="claude-fable-5-1")
    assert event.requested_model == "claude-fable-5-1"
    assert usage_history.list_events()[0].requested_model == "claude-fable-5-1"


def test_requested_model_is_left_out_when_it_matches_what_served(env):
    # The ordinary case stays null, so a non-null value always means "translated".
    event = usage_history.record("prof-a", None, "claude-sonnet-5",
                                 {"input_tokens": 1, "output_tokens": 1},
                                 requested_model="claude-sonnet-5")
    assert event.requested_model is None
    assert usage_history.list_events()[0].requested_model is None




# ---- stats aggregation (backs /api/usage/stats) ---------------------------

def _ev(**kw):
    base = dict(timestamp="2026-09-10T10:00:00+00:00", profile_id="p1", project_id="-Users-a-app",
                model="gpt-5.6-sol", input_tokens=100, output_tokens=50,
                cache_creation_input_tokens=10, cache_read_input_tokens=400, cost_usd=0.5,
                requested_model=None)
    base.update(kw)
    return usage_history.UsageEvent(**base)


def test_totals_reports_cache_hit_and_counts_unpriced_events():
    events = [_ev(), _ev(cost_usd=None, model="mystery-model")]
    t = usage_history.totals(events)
    assert t["requests"] == 2 and t["cost_usd"] == 0.5
    assert t["uncosted_events"] == 1          # surfaced, never silently dropped from the total
    assert t["tokens_in"] == 200 and t["tokens_out"] == 100
    assert t["cache_hit_percent"] == round(800 / (200 + 20 + 800) * 100, 1)


def test_totals_of_nothing_has_no_cache_percentage():
    t = usage_history.totals([])
    assert t["requests"] == 0 and t["cost_usd"] == 0 and t["cache_hit_percent"] is None


def test_split_by_ranks_by_cost_and_adds_shares():
    events = [_ev(model="a", cost_usd=3.0), _ev(model="b", cost_usd=1.0), _ev(model="b", cost_usd=0.5)]
    rows = usage_history.split_by(events, "model")
    assert [r["key"] for r in rows] == ["a", "b"]
    assert rows[0]["share"] == 66.7 and rows[1]["requests"] == 2


def test_split_by_folds_the_tail_into_other():
    events = [_ev(model=f"m{i}", cost_usd=float(10 - i)) for i in range(9)]
    rows = usage_history.split_by(events, "model", top=3)
    assert len(rows) == 4 and rows[-1]["other"] is True
    assert rows[-1]["requests"] == 6                      # every tail row is still counted
    assert round(sum(r["share"] for r in rows)) == 100


def test_split_by_requested_model_falls_back_to_what_served_it():
    """Only codex rows carry requested_model; an Anthropic row asked for the
    model it got, so it must not collapse into an empty bucket."""
    events = [_ev(model="gpt-6-astra", requested_model="claude-fable-5-1"), _ev(model="claude-sonnet-5")]
    keys = {r["key"] for r in usage_history.split_by(events, "requested_model")}
    assert keys == {"claude-fable-5-1", "claude-sonnet-5"}


def test_split_by_unknown_dimension_is_empty_not_an_error():
    assert usage_history.split_by([_ev()], "nonsense") == []


def test_history_begins_is_the_oldest_event(env):
    assert usage_history.history_begins([]) is None
    usage_history.record("p", None, "m", {"input_tokens": 1})
    usage_history.record("p", None, "m", {"input_tokens": 1})
    events = usage_history.list_events()
    assert usage_history.history_begins(events) == events[0].timestamp


# ---- series_by_kind: one cost line per provider over shared buckets ----

def test_series_splits_cost_by_provider_kind():
    now = datetime.now().astimezone()
    events = [
        _event_at(now, profile_id="codex-1", cost_usd=2.0),
        _event_at(now, profile_id="claude-1", cost_usd=0.5),
    ]
    buckets = usage_history.daily_totals(events, days=3)
    series = usage_history.series_by_kind(events, buckets,
                                          {"codex-1": "codex", "claude-1": "oauth"})
    by_kind = {s["kind"]: s["points"] for s in series}
    assert set(by_kind) == {"codex", "oauth"}
    # Every series spans the same buckets, so the lines share an x-axis.
    assert all(len(points) == len(buckets) for points in by_kind.values())
    assert sum(by_kind["codex"]) == 2.0
    assert sum(by_kind["oauth"]) == 0.5


def test_series_places_events_in_the_bucket_that_contains_them():
    # Month buckets are keyed by the 1st and week buckets by the window's
    # first day. Matching an event's own date against those keys dropped
    # everything that did not land exactly on a boundary, which silently
    # emptied the 1y and 3m series.
    now = datetime.now().astimezone()
    events = [_event_at(now, profile_id="codex-1", cost_usd=3.0)]

    for buckets in (usage_history.monthly_totals(events, months=3),
                    usage_history.weekly_totals(events, weeks=3)):
        series = usage_history.series_by_kind(events, buckets, {"codex-1": "codex"})
        assert series, "the event must land in some bucket"
        points = series[0]["points"]
        assert len(points) == len(buckets)
        assert sum(points) == 3.0


def test_series_attributes_an_unknown_profile_to_other():
    now = datetime.now().astimezone()
    events = [_event_at(now, profile_id="deleted-profile", cost_usd=1.0)]
    buckets = usage_history.daily_totals(events, days=2)
    series = usage_history.series_by_kind(events, buckets, {})
    # A deleted profile still spent money; dropping it would make the lines
    # disagree with the total beside them.
    assert [s["kind"] for s in series] == ["other"]
    assert sum(series[0]["points"]) == 1.0


def test_series_is_empty_without_buckets():
    assert usage_history.series_by_kind([], [], {}) == []


def test_chart_tables_cover_every_range_and_stay_finer_than_the_bar_chart():
    # The line chart renders more points than the bar chart can, so it has its
    # own tables. Keeping them separate is the point: widening RANGE_TO_DAYS
    # instead would have let /api/usage/summary render 91 bars.
    assert set(usage_history.CHART_GRANULARITY.keys()) == set(usage_history.RANGE_KEYS)
    assert set(usage_history.CHART_GRANULARITY.values()) <= {"hour", "day", "week", "month"}
    chart_day_ranges = {k for k, v in usage_history.CHART_GRANULARITY.items() if v == "day"}
    assert set(usage_history.CHART_TO_DAYS.keys()) == chart_day_ranges
    # The bar chart's own bound is untouched.
    assert all(1 <= d <= 31 for d in usage_history.RANGE_TO_DAYS.values())


# ---- ECO savings aggregate ----

def _eco_ev(bytes_saved=0, tokens_saved=0, model="claude-sonnet-5", mode=None):
    return _event_at(datetime.now().astimezone(), model=model,
                     eco_bytes_saved=bytes_saved or None,
                     eco_tokens_saved=tokens_saved or None,
                     eco_mode=mode)


def test_eco_totals_distinguishes_never_ran_from_saved_nothing():
    # These mean very different things to someone deciding whether to enable
    # it, so the UI must be able to tell them apart.
    never = usage_history.eco_totals([])
    assert never["requests"] == 0 and never["bytes"] == 0 and never["cost_usd"] is None
    ran_nothing = usage_history.eco_totals([_eco_ev()])
    assert ran_nothing["requests"] == 0


def test_eco_totals_sums_bytes_and_tokens():
    out = usage_history.eco_totals([
        _eco_ev(1000, 250, mode="light"),
        _eco_ev(2000, 500, mode="light"),
    ])
    assert out["requests"] == 2
    assert out["bytes"] == 3000
    assert out["tokens"] == 750
    assert out["modes"] == {"light": 2}


def test_eco_cost_prices_saved_tokens_at_the_input_rate():
    from claude_unlimited import pricing
    rate = pricing.find_price("claude-sonnet-5").input_per_mtok
    out = usage_history.eco_totals([_eco_ev(50_000, 200_000, mode="light")])
    # Saved tokens are INPUT tokens; prices are per MILLION.
    assert out["cost_usd"] == round(200_000 * rate / 1_000_000, 4)


def test_eco_cost_is_none_when_nothing_could_be_priced():
    # An estimate of an estimate: better absent than invented.
    out = usage_history.eco_totals([_eco_ev(100, 50, model="some-unknown-model", mode="light")])
    assert out["bytes"] == 100
    assert out["cost_usd"] is None


def test_eco_modes_are_ordered_deterministically():
    out = usage_history.eco_totals([
        _eco_ev(10, 5, mode="light"), _eco_ev(10, 5, mode="aggressive"),
    ])
    assert list(out["modes"]) == sorted(out["modes"])


def test_daily_totals_counts_requests_as_well_as_tokens():
    # The activity heatmap is "calls per day"; tokens alone cannot draw it.
    # Existing callers must keep getting `tokens`.
    now = datetime.now().astimezone()
    rows = usage_history.daily_totals([_event_at(now), _event_at(now)], days=3)
    today = rows[-1]
    assert today["requests"] == 2
    assert "tokens" in today and today["tokens"] > 0
    assert all("requests" in r for r in rows)
    assert rows[0]["requests"] == 0          # quiet days are 0, not missing


# ---- the SQL aggregates the once-a-second paths use ------------------------
#
# Each one has to agree with the pure helper it replaced, because the pure
# helpers are what every other test (and the Statistics page) is written
# against. These compare the two on the same data rather than restating
# expected numbers, so a future change to either side has to keep them equal.

def _a_spread_of_events():
    usage_history.record("prof-a", "-Users-a-app", "claude-sonnet-5",
                         {"input_tokens": 1000, "output_tokens": 100})
    usage_history.record("prof-a", "-Users-a-app", "totally-unknown-model",
                         {"input_tokens": 50, "output_tokens": 5})
    usage_history.record("prof-b", None, "claude-haiku-4-5",
                         {"input_tokens": 700, "output_tokens": 70})
    usage_history.record("prof-b", "-Users-a-other", "claude-sonnet-5",
                         {"input_tokens": 2, "output_tokens": 2},
                         requested_model="claude-opus-5")


def test_totals_by_profile_matches_the_pure_helper(env):
    _a_spread_of_events()
    assert usage_history.totals_by_profile() == usage_history.usage_by_profile(usage_history.list_events())


def test_totals_by_profile_is_empty_with_no_events(env):
    assert usage_history.totals_by_profile() == {}


def test_totals_by_project_matches_the_pure_helper(env):
    _a_spread_of_events()
    assert usage_history.totals_by_project() == usage_history.tokens_by_project(usage_history.list_events())


def test_latest_by_profile_matches_the_pure_helper(env):
    _a_spread_of_events()
    assert usage_history.latest_by_profile() == usage_history.last_use_by_profile(usage_history.list_events())


def test_latest_by_profile_orders_by_real_time_not_by_the_stored_string(env):
    """A row imported from the old JSONL log can carry a local offset. Sorted
    as text, "+03:00" would win over a newer UTC row; sorted as time, it must
    not."""
    db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens)"
               " VALUES (?, ?, ?, ?, ?)", ("2026-01-01T12:00:00+03:00", "prof-a", "old-model", 1, 1))
    db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens)"
               " VALUES (?, ?, ?, ?, ?)", ("2026-01-01T10:00:00+00:00", "prof-a", "new-model", 1, 1))
    assert usage_history.latest_by_profile()["prof-a"]["model"] == "new-model"


def test_events_since_matches_filtering_the_whole_table(env):
    now = datetime.now(timezone.utc)
    for hours, profile in ((0, "prof-a"), (2, "prof-a"), (40, "prof-b"), (24 * 20, "prof-b")):
        db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens)"
                   " VALUES (?, ?, ?, ?, ?)",
                   ((now - timedelta(hours=hours)).isoformat(), profile, "claude-sonnet-5", 10, 1))
    for range_key in ("1h", "1d", "1w", "1m", "all"):
        expected = usage_history.filter_events_since(usage_history.list_events(), range_key)
        assert usage_history.events_since(range_key) == expected, range_key


def test_event_count_and_history_begins_match_the_list(env):
    assert usage_history.event_count() == 0 and usage_history.history_begins_at() is None
    _a_spread_of_events()
    events = usage_history.list_events()
    assert usage_history.event_count() == len(events)
    assert usage_history.history_begins_at() == usage_history.history_begins(events)


def test_events_in_last_days_covers_the_days_the_chart_buckets(env):
    now = datetime.now().astimezone()
    for days in (0, 1, 6, 29):
        db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens)"
                   " VALUES (?, ?, ?, ?, ?)",
                   ((now - timedelta(days=days)).isoformat(), "prof-a", "claude-sonnet-5", 10, 1))
    for days in (1, 7, 30):
        from_store = usage_history.daily_totals_by_profile(
            usage_history.events_in_last_days(days), days=days)
        from_all = usage_history.daily_totals_by_profile(usage_history.list_events(), days=days)
        assert from_store == from_all, days


# ---- pre-aggregated rows must report exactly what the events do ------------

def _busy_history(n=240):
    """Events that repeat within a minute (so they fold) and vary across every
    field a breakdown groups by (so folding the wrong ones would show)."""
    import random
    random.seed(7)
    models = ["claude-sonnet-5", "claude-haiku-4-5", "totally-unknown-model"]
    now = datetime.now(timezone.utc)
    for i in range(n):
        model = models[i % len(models)]
        ts = (now - timedelta(minutes=(i % 10) * 3, seconds=i % 59)).isoformat()
        db.execute(
            """INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens,
                                        cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
                                        requested_model, eco_bytes_saved, eco_tokens_saved, eco_mode,
                                        speech_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, f"prof-{i % 2}", f"-proj-{i % 2}" if i % 5 else None, model,
             100 + i, 10 + i, i % 7, i % 11,
             None if model == "totally-unknown-model" else round(0.001 * (i + 1), 6),
             "claude-opus-5" if i % 6 == 0 else None,
             (i * 13) if i % 4 == 0 else None, (i * 3) if i % 4 == 0 else None,
             "light" if i % 4 == 0 else None,
             ("full" if i % 3 == 0 else "lite") if i % 2 == 0 else None))


def test_aggregated_rows_fold_but_report_the_same_numbers(env):
    _busy_history()
    raw = usage_history.events_since("1d")
    folded = usage_history.aggregated_since("1d")
    assert 0 < len(folded) < len(raw), "nothing folded — the test data is wrong"

    assert usage_history.totals(folded) == usage_history.totals(raw)
    for dimension in ("model", "project", "profile", "requested_model"):
        assert usage_history.split_by(folded, dimension) == usage_history.split_by(raw, dimension), dimension
    assert usage_history.model_split(folded) == usage_history.model_split(raw)
    assert usage_history.hourly_histogram(folded) == usage_history.hourly_histogram(raw)
    assert usage_history.cost_by_profile(folded) == usage_history.cost_by_profile(raw)
    assert usage_history.daily_totals(folded, days=7) == usage_history.daily_totals(raw, days=7)
    assert usage_history.daily_totals_by_profile(folded) == usage_history.daily_totals_by_profile(raw)
    assert usage_history.hourly_totals(folded, bucket_hours=6) == usage_history.hourly_totals(raw, bucket_hours=6)
    assert usage_history.weekly_totals(folded) == usage_history.weekly_totals(raw)
    assert usage_history.monthly_totals(folded) == usage_history.monthly_totals(raw)
    assert usage_history.eco_totals(folded) == usage_history.eco_totals(raw)
    assert usage_history.speech_totals(folded) == usage_history.speech_totals(raw)
    buckets = usage_history.daily_totals(raw, days=7)
    kinds = {"prof-0": "oauth", "prof-1": "codex", "prof-2": "api"}
    assert (usage_history.series_by_kind(folded, buckets, kinds)
            == usage_history.series_by_kind(raw, buckets, kinds))


def test_aggregated_since_holds_the_range_boundary_exactly(env):
    """The fold rounds a row's timestamp down to its minute. The range cutoff
    is therefore applied to real timestamps, before folding — otherwise a
    request just inside the window would be judged by its rounded edge."""
    now = datetime.now(timezone.utc)
    inside = now - timedelta(minutes=59, seconds=30)
    outside = now - timedelta(minutes=60, seconds=30)
    for ts in (inside, outside):
        db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens)"
                   " VALUES (?, ?, ?, ?, ?)", (ts.isoformat(), "prof-a", "claude-sonnet-5", 10, 1))
    assert usage_history.totals(usage_history.aggregated_since("1h"))["requests"] == 1


def test_aggregated_rows_never_merge_across_a_breakdown_key(env):
    """Same minute, everything else different: nothing may fold."""
    stamp = datetime.now(timezone.utc).replace(second=1).isoformat()
    rows = [("prof-a", "claude-sonnet-5", "-proj-1"), ("prof-b", "claude-sonnet-5", "-proj-1"),
            ("prof-a", "claude-haiku-4-5", "-proj-1"), ("prof-a", "claude-sonnet-5", "-proj-2")]
    for profile, model, project in rows:
        db.execute("INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens)"
                   " VALUES (?, ?, ?, ?, ?, ?)", (stamp, profile, project, model, 10, 1))
    assert len(usage_history.aggregated_since("1d")) == len(rows)


def test_aggregated_rows_keep_priced_and_unpriced_requests_apart(env):
    """`uncosted_events` counts requests with no published price. Folding one
    in with a priced request would hide it."""
    stamp = datetime.now(timezone.utc).replace(second=2).isoformat()
    for model in ("claude-sonnet-5", "totally-unknown-model"):
        usage_history.record("prof-a", None, model, {"input_tokens": 10, "output_tokens": 1})
        db.execute("UPDATE usage_event SET ts = ? WHERE ts = (SELECT MAX(ts) FROM usage_event)", (stamp,))
    totals = usage_history.totals(usage_history.aggregated_since("1d"))
    assert totals["requests"] == 2 and totals["uncosted_events"] == 1
