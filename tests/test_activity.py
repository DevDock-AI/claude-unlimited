import pytest

import claude_unlimited.activity as activity


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


def test_log_trims_to_max_events(monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS", 5)
    for i in range(10):
        activity.record("config", f"event-{i}")
    events = activity.list_events(limit=100)
    assert len(events) == 5
    assert events[0].text == "event-9"  # newest first
    assert events[-1].text == "event-5"


def test_corrupt_line_does_not_break_the_whole_log(tmp_path):
    activity.record("config", "good-1")
    with activity.ACTIVITY_FILE.open("a") as f:
        f.write("not valid json at all\n")
    activity.record("config", "good-2")
    events = activity.list_events()
    assert [e.text for e in events] == ["good-2", "good-1"]


def test_list_events_since_excludes_earlier_events():
    import json

    activity.ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with activity.ACTIVITY_FILE.open("a") as f:
        for day, text in [("18", "day-18"), ("19", "day-19"), ("20", "day-20")]:
            f.write(json.dumps({"timestamp": f"2026-08-{day}T00:00:00+00:00", "category": "config",
                                 "text": text, "meta": None}) + "\n")

    events = activity.list_events(since="2026-08-19T00:00:00+00:00")
    assert [e.text for e in events] == ["day-20", "day-19"]


def test_list_events_until_excludes_later_events():
    import json

    activity.ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with activity.ACTIVITY_FILE.open("a") as f:
        for day, text in [("18", "day-18"), ("19", "day-19"), ("20", "day-20")]:
            f.write(json.dumps({"timestamp": f"2026-08-{day}T00:00:00+00:00", "category": "config",
                                 "text": text, "meta": None}) + "\n")

    events = activity.list_events(until="2026-08-19T00:00:00+00:00")
    assert [e.text for e in events] == ["day-19", "day-18"]


# --- the log must never be able to break what it is logging ----------------


def test_an_unwritable_log_does_not_raise(monkeypatch, tmp_path):
    """record() runs inside the live request path, after the upstream response
    has already come back. A full disk turning a successful request into a
    dropped connection is a far worse failure than a missing audit line."""
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")

    def no_disk(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(activity.ACTIVITY_FILE.__class__, "open", no_disk)
    event = activity.record("rotation", "switched to B")
    assert event.text == "switched to B"  # still returns the event it built


def test_a_bad_category_still_raises(monkeypatch, tmp_path):
    """A programming error, not an environmental one — it must not be
    swallowed alongside the I/O guard."""
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    with pytest.raises(ValueError):
        activity.record("not-a-category", "x")


def test_a_line_with_the_wrong_shape_is_skipped_not_fatal(monkeypatch, tmp_path):
    """Only json.JSONDecodeError used to be caught, so a line that parsed but
    did not match the dataclass — an unknown field from a newer version, a
    bare number — raised TypeError and 500'd the whole Activity page."""
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    log = tmp_path / "activity.jsonl"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log)

    activity.record("rotation", "good one")
    with log.open("a", encoding="utf-8") as f:
        f.write('{"timestamp":"2026-01-01T00:00:00+00:00","category":"rotation",'
                '"text":"from the future","severity":"high"}\n')   # unknown field
        f.write('{"timestamp":"2026-01-01T00:00:00+00:00"}\n')     # missing fields
        f.write('123\n')                                            # not an object
        f.write('not json at all\n')
    activity.record("config", "another good one")

    texts = [e.text for e in activity.list_events()]
    assert texts == ["another good one", "good one"]
