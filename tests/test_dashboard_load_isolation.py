"""The Dashboard must stay quick while the daemon is busy.

Regression for a Profiles page that sat on "Loading" for minutes. Every read
and write shared one lock, and each `GET /api/profiles` scanned the whole usage
table, so a few open tabs plus one slow report queued faster than the lock
drained — and the proxy's own usage writes queued behind them. These tests hold
a slow report, and a streaming completion, open and require the Profiles list to
answer anyway. Nothing here touches the network.
"""

import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.db as db
import claude_unlimited.usage_history as usage_history
from claude_unlimited import placeholder_token
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.upstream import UpstreamResponse

QUICK = 0.5          # a Dashboard poll that takes longer than this has queued
SLOW = 1.5           # how long the simulated report holds its read


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    db.close_this_thread()
    yield tmp_path
    db.close_this_thread()


@pytest.fixture
def slow_sql(monkeypatch):
    """`SELECT slow(1.5)` takes 1.5 s inside SQLite on whichever read handle
    runs it — a stand-in for a report over a large history."""
    real = db._ReadPool._connect

    def connect(self):
        conn = real(self)
        conn.create_function("slow", 1, lambda s: time.sleep(s) or 1)
        return conn

    monkeypatch.setattr(db._ReadPool, "_connect", connect)


def _timed(fn):
    t = time.monotonic()
    out = fn()
    return time.monotonic() - t, out


def test_a_slow_read_does_not_hold_up_writes_or_other_reads(env, slow_sql):
    db.connect()
    reader = threading.Thread(target=lambda: db.query(f"SELECT slow({SLOW})"))
    reader.start()
    time.sleep(0.2)  # the slow read now has its handle
    write, rowid = _timed(lambda: db.execute(
        "INSERT INTO activity_event (ts, category, text) VALUES ('2026-10-08T00:00:00+00:00', 'x', 'y')"))
    read, rows = _timed(lambda: db.query("SELECT COUNT(*) AS n FROM activity_event"))
    reader.join()
    assert rowid is not None and rows[0]["n"] == 1
    assert write < QUICK, f"a usage/activity write waited {write:.2f}s behind a report"
    assert read < QUICK, f"a cheap read waited {read:.2f}s behind a report"


def test_read_handles_are_bounded(env, slow_sql):
    """Thread-per-connection must not become handle-per-connection: that leak is
    what the single shared connection was introduced to end."""
    db.connect()
    threads = [threading.Thread(target=lambda: db.query("SELECT slow(0.3)"))
               for _ in range(db.READ_POOL_SIZE * 3)]
    for t in threads:
        t.start()
    time.sleep(0.15)
    assert db._pool._open <= db.READ_POOL_SIZE
    for t in threads:
        t.join()
    assert db._pool._open <= db.READ_POOL_SIZE


def test_a_read_sees_what_the_writer_just_committed(env):
    for i in range(20):
        db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, 'x', 'y')",
                   (f"2026-10-08T00:00:{i:02d}+00:00",))
        assert db.query("SELECT COUNT(*) AS n FROM activity_event")[0]["n"] == i + 1


def _record(profile, tokens, model="claude-sonnet-5-5", cost_model=True):
    return usage_history.record(profile, "-proj", model if cost_model else "unpriced-model",
                                {"input_tokens": tokens, "output_tokens": 0})


def _reference():
    """What the table says, computed the slow way: from every event."""
    events = usage_history.list_events()
    return (usage_history.usage_by_profile(events), usage_history.tokens_by_project(events))


def test_the_rollup_matches_the_table_as_rows_are_added(env):
    for i in range(3):
        _record("a", 10 + i)
    assert (usage_history.totals_by_profile(), usage_history.totals_by_project()) == _reference()
    _record("b", 5)
    _record("a", 7, cost_model=False)
    assert (usage_history.totals_by_profile(), usage_history.totals_by_project()) == _reference()
    assert usage_history.totals_by_profile()["b"]["tokens"] == 5


def test_the_rollup_starts_again_after_a_reset(env):
    _record("a", 100)
    assert usage_history.totals_by_profile()["a"]["tokens"] == 100
    usage_history.reset()
    assert usage_history.totals_by_profile() == {}
    _record("a", 3)
    assert usage_history.totals_by_profile()["a"]["tokens"] == 3


def test_latest_by_profile_orders_by_real_time_not_by_offset_text(env):
    """An import from the old log can carry a local offset; its text sorts
    ahead of a newer UTC row but it is the older request."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    older_local = (now - timedelta(hours=5)).astimezone(timezone(timedelta(hours=9)))
    db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens) VALUES (?, 'a', 'newer', 1, 1)",
               (now.isoformat(),))
    assert usage_history.latest_by_profile()["a"]["model"] == "newer"       # builds the rollup
    db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens) VALUES (?, 'a', 'older', 1, 1)",
               (older_local.isoformat(),))
    assert usage_history.latest_by_profile()["a"]["model"] == "newer"       # folded incrementally
    db.execute("INSERT INTO usage_event (ts, profile_id, model, input_tokens, output_tokens) VALUES (?, 'a', 'newest', 1, 1)",
               ((now + timedelta(seconds=30)).isoformat(),))
    assert usage_history.latest_by_profile()["a"]["model"] == "newest"


def test_a_poll_after_the_first_does_not_scan_the_table(env, monkeypatch):
    """The cost of a Dashboard poll must follow what happened since the last
    one, not how long the history is."""
    for i in range(50):
        _record(f"p{i % 3}", i)
    usage_history.totals_by_profile()                        # warm
    seen = []
    real = db.query
    monkeypatch.setattr(db, "query", lambda sql, params=(): (seen.append(sql), real(sql, params))[1])
    usage_history.totals_by_profile()
    usage_history.latest_by_profile()
    usage_history.totals_by_project()
    assert len(seen) == 3 and all("WHERE id > ?" in q for q in seen), seen
    assert not any("GROUP BY" in q or "ROW_NUMBER" in q for q in seen)


# ---- end to end: the real HTTP server, a held stream, a slow report --------

class _Conn:
    def close(self):
        pass


@pytest.fixture
def busy_daemon(monkeypatch, tmp_path, slow_sql):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr(placeholder_token, "APP_DIR", tmp_path)
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")

    class Store:
        def get_token(self, profile_id):
            return "real-token"

    monkeypatch.setattr("claude_unlimited.gateway.secret_store", Store())
    db.close_this_thread()
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True),
                             Profile(id="b", name="B", kind="oauth", automatic=True, enabled=True)]))
    for i in range(200):  # enough history that a full scan would be visible in the SQL asserts above
        usage_history.record("a" if i % 2 else "b", "-proj", "claude-sonnet-5-5",
                             {"input_tokens": 10, "output_tokens": 5})

    release = threading.Event()
    started = threading.Event()

    def transport(req):
        def chunks():
            yield (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-5-5",'
                   b'"usage":{"input_tokens":3,"output_tokens":1}}}\n\n')
            started.set()
            release.wait(30)              # the slow upstream: a stream held open
            yield (b'event: message_delta\ndata: {"type":"message_delta","delta":{},'
                   b'"usage":{"input_tokens":3,"output_tokens":9}}\n\n')
        return UpstreamResponse(status=200, headers={"content-type": "text/event-stream"},
                                body_chunks=chunks(), connection=_Conn())

    server = daemon.make_server(host="127.0.0.1", port=0)
    daemon._gateway._transport = transport
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, release, started
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        db.close_this_thread()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        return json.loads(resp.read())


def test_profiles_answer_quickly_while_a_stream_is_held_and_reports_are_running(busy_daemon, monkeypatch):
    base, release, started = busy_daemon

    summaries = []

    def slow_report(range_key):
        summaries.append(range_key)
        db.query(f"SELECT slow({SLOW})")       # the report's read, on a read handle
        return []

    monkeypatch.setattr(usage_history, "aggregated_since", slow_report)
    monkeypatch.setattr(usage_history, "aggregated_in_last_days", lambda days: [])

    # 1. a proxied completion that the upstream keeps open
    token = placeholder_token.get_or_create()
    stream_done = threading.Event()

    def stream():
        req = urllib.request.Request(
            base + "/v1/messages", method="POST", headers={"Authorization": f"Bearer {token}"},
            data=json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 5, "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}).encode())
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        stream_done.set()

    threading.Thread(target=stream, daemon=True).start()
    assert started.wait(10), "the held stream never reached the upstream"

    # 2. several open Dashboard tabs all asking for the same report
    reports = [threading.Thread(target=lambda: _get(base, "/api/usage/summary?range=1w"), daemon=True)
               for _ in range(4)]
    for r in reports:
        r.start()
    time.sleep(0.3)

    # 3. the Profiles page polls meanwhile
    worst = 0.0
    for _ in range(5):
        took, body = _timed(lambda: _get(base, "/api/profiles"))
        worst = max(worst, took)
        assert {p["id"] for p in body["profiles"]} == {"a", "b"}
        assert sum(p["tokens_total"] for p in body["profiles"]) == 200 * 15
    status, _ = _timed(lambda: _get(base, "/api/status"))
    activity_took, _ = _timed(lambda: _get(base, "/api/activity?limit=8"))

    assert worst < QUICK, f"/api/profiles took {worst:.2f}s while a report and a stream were in flight"
    assert activity_took < QUICK

    for r in reports:
        r.join(timeout=20)
    assert len(summaries) == 1, f"four identical reports ran {len(summaries)} times; they should share one run"

    # the held stream is unaffected and still completes, and its usage lands
    assert not stream_done.is_set()
    release.set()
    assert stream_done.wait(10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sum(p["tokens_total"] for p in _get(base, "/api/profiles")["profiles"]) == 200 * 15 + 12:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the held stream's usage never reached the Profiles totals")
