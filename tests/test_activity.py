import pytest

import claude_unlimited.activity as activity
import claude_unlimited.db as db


@pytest.fixture(autouse=True)
def isolated_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")


def test_record_and_list_roundtrip():
    activity.record("session", "Session connected", meta="profile=a")
    events = activity.list_events()
    assert len(events) == 1
    assert events[0].text == "Session connected"
    assert events[0].meta == "profile=a"


def test_list_events_newest_first():
    activity.record("config", "first")
    activity.record("config", "second")
    activity.record("config", "third")
    events = activity.list_events()
    assert [e.text for e in events] == ["third", "second", "first"]


def test_list_events_filters_by_category():
    activity.record("rotation", "rotated")
    activity.record("session", "connected")
    events = activity.list_events(category="rotation")
    assert len(events) == 1
    assert events[0].category == "rotation"


def test_unknown_category_rejected():
    with pytest.raises(ValueError):
        activity.record("not-a-real-category", "x")


def test_list_events_on_empty_log_returns_empty_list():
    assert activity.list_events() == []


def test_history_is_kept_rather_than_trimmed(monkeypatch):
    """The store replaced a log that discarded everything past MAX_EVENTS —
    keeping history is the whole point of the move, so the constant survives
    only as a read cap."""
    monkeypatch.setattr(activity, "MAX_EVENTS", 5)
    for i in range(10):
        activity.record("config", f"event-{i}")
    events = activity.list_events(limit=100)
    assert len(events) == 10
    assert events[0].text == "event-9" and events[-1].text == "event-0"


def _seed(day_texts):
    for day, text in day_texts:
        db.execute("INSERT INTO activity_event (ts, category, text, meta) VALUES (?, ?, ?, ?)",
                   (f"2026-08-{day}T00:00:00+00:00", "config", text, None))


def test_list_events_since_excludes_earlier_events():
    _seed([("18", "day-18"), ("19", "day-19"), ("20", "day-20")])

    events = activity.list_events(since="2026-08-19T00:00:00+00:00")
    assert [e.text for e in events] == ["day-20", "day-19"]


def test_list_events_until_excludes_later_events():
    _seed([("18", "day-18"), ("19", "day-19"), ("20", "day-20")])

    events = activity.list_events(until="2026-08-19T00:00:00+00:00")
    assert [e.text for e in events] == ["day-19", "day-18"]


# --- the log must never be able to break what it is logging ----------------


def test_an_unusable_store_does_not_raise(monkeypatch, tmp_path):
    """record() runs inside the live request path, after the upstream response
    has already come back. A store that cannot be written turning a successful
    request into a dropped connection is far worse than a missing audit line."""
    db.close_this_thread()
    db.path().parent.mkdir(parents=True, exist_ok=True)
    db.path().write_text("this is not a database")

    event = activity.record("rotation", "switched to B")

    assert event.text == "switched to B"  # still returns the event it built
    assert activity.list_events() == []   # and reading degrades quietly too


def test_a_bad_category_still_raises(monkeypatch, tmp_path):
    """A programming error, not an environmental one — it must not be
    swallowed alongside the I/O guard."""
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    with pytest.raises(ValueError):
        activity.record("not-a-category", "x")


