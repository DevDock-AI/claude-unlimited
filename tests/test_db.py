"""The SQLite store: migrations, isolation, threading, and degraded mode."""

import sqlite3
import threading

import pytest

import claude_unlimited.db as db


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    db.close_this_thread()
    yield tmp_path
    db.close_this_thread()


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_fresh_database_is_created_and_migrated(env):
    conn = db.connect()
    assert conn is not None
    assert _tables(conn) >= {"usage_event", "activity_event"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.path().exists() and db.available()


def test_schema_v1_carries_every_column_the_plan_specifies(env):
    conn = db.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_event)")}
    assert cols >= {"ts", "profile_id", "project_id", "model", "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd",
                    "requested_model", "eco_bytes_saved", "eco_tokens_saved", "eco_mode"}


def test_connecting_twice_does_not_re_run_migrations(env):
    db.connect()
    db.close_this_thread()
    conn = db.connect()  # a second open of the SAME file must be a no-op, not an error
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_each_app_dir_gets_its_own_database(env, monkeypatch, tmp_path):
    db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, ?, ?)", ("t", "config", "first"))
    other = tmp_path / "another-home"
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", other)
    # The connection is keyed by resolved path, so moving APP_DIR (which every
    # test fixture does) must not keep reading the previous database.
    assert db.query("SELECT * FROM activity_event") == []
    assert db.path().parent == other


def test_writes_and_reads_round_trip(env):
    rowid = db.execute(
        "INSERT INTO usage_event (ts, profile_id, model, input_tokens, requested_model) VALUES (?, ?, ?, ?, ?)",
        ("2026-09-16T10:00:00+00:00", "p1", "gpt-6-astra", 42, "claude-fable-5-1"))
    assert rowid == 1
    rows = db.query("SELECT * FROM usage_event WHERE profile_id = ?", ("p1",))
    assert len(rows) == 1
    assert rows[0]["model"] == "gpt-6-astra" and rows[0]["requested_model"] == "claude-fable-5-1"
    assert rows[0]["eco_bytes_saved"] is None


def test_wal_is_enabled(env):
    assert db.connect().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_every_thread_can_write(env):
    errors = []

    def writer(n):
        try:
            db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, ?, ?)",
                       (f"t{n}", "config", f"from thread {n}"))
        except Exception as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(db.query("SELECT * FROM activity_event")) == 4


def test_threads_share_one_connection_and_leave_no_handle_behind(env):
    """The daemon serves a thread per HTTP connection. A handle per thread
    meant a handle per Dashboard poll, none of them ever closed: each one held
    a WAL read mark, the log could not be checkpointed past them, and opening
    the next handle over that log took seconds. One shared connection is what
    makes that impossible."""
    handles = []

    def worker():
        handles.append(db.connect())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(set(id(h) for h in handles)) == 1
    assert handles[0] is db.connect()


def test_closing_truncates_the_write_ahead_log(env):
    for n in range(50):
        db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, ?, ?)",
                   (f"t{n}", "config", "x" * 500))
    wal = db.path().with_name(db.path().name + "-wal")
    db.close_this_thread()
    # Gone, or truncated to nothing: either way the next open has no log to
    # rebuild an index over.
    assert not wal.exists() or wal.stat().st_size == 0


def test_an_unusable_database_degrades_instead_of_raising(env):
    db.close_this_thread()
    db.path().write_text("this is not a database")

    assert db.connect() is None
    assert db.available() is False and "atabase" in (db.degraded_reason() or "")
    # The whole point: neither call raises into the request path.
    assert db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, ?, ?)", ("t", "config", "x")) is None
    assert db.query("SELECT * FROM activity_event") == []


def test_a_newer_schema_degrades_rather_than_guessing(env):
    conn = sqlite3.connect(db.path())
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()
    db.close_this_thread()

    assert db.connect() is None
    assert "newer than this build" in (db.degraded_reason() or "")


# ---- one-shot import of the JSONL logs the store replaces -----------------

def _write_jsonl(path, rows):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _usage_row(ts, tokens=10, **kw):
    row = {"timestamp": ts, "profile_id": "p1", "project_id": "-Users-a-app", "model": "claude-sonnet-5",
           "input_tokens": tokens, "output_tokens": 2, "cache_creation_input_tokens": 0,
           "cache_read_input_tokens": 0, "cost_usd": 0.01}
    row.update(kw)
    return row


def test_import_moves_both_logs_into_the_store(env):
    _write_jsonl(env / db.LEGACY_USAGE_BASENAME,
                 [_usage_row("2026-09-01T10:00:00+00:00"),
                  _usage_row("2026-09-01T11:00:00+00:00", model="gpt-6-astra",
                             requested_model="claude-fable-5-1")])
    _write_jsonl(env / db.LEGACY_ACTIVITY_BASENAME,
                 [{"timestamp": "2026-09-01T10:00:00+00:00", "category": "rotation",
                   "text": "Rotated A -> B", "meta": None}])

    result = db.import_legacy_logs()

    assert result["ok"] and result["usage_imported"] == 2 and result["activity_imported"] == 1
    rows = db.query("SELECT * FROM usage_event ORDER BY ts")
    assert [r["model"] for r in rows] == ["claude-sonnet-5", "gpt-6-astra"]
    assert rows[1]["requested_model"] == "claude-fable-5-1"
    assert rows[0]["project_id"] == "-Users-a-app" and rows[0]["cost_usd"] == 0.01
    assert db.query("SELECT text FROM activity_event")[0]["text"] == "Rotated A -> B"


def test_importing_twice_cannot_double_count(env):
    """The gate for this phase: a re-run, or a user restoring a backup of the
    old log, must not duplicate a single row."""
    rows = [_usage_row("2026-09-01T10:00:00+00:00"), _usage_row("2026-09-01T11:00:00+00:00")]
    _write_jsonl(env / db.LEGACY_USAGE_BASENAME, rows)
    db.import_legacy_logs()

    _write_jsonl(env / db.LEGACY_USAGE_BASENAME, rows)  # the same file is back
    second = db.import_legacy_logs()

    assert second["usage_imported"] == 0 and second["usage_skipped"] == 2
    assert len(db.query("SELECT * FROM usage_event")) == 2


def test_sources_are_retired_not_deleted(env):
    _write_jsonl(env / db.LEGACY_USAGE_BASENAME, [_usage_row("2026-09-01T10:00:00+00:00")])
    db.import_legacy_logs()

    assert not (env / db.LEGACY_USAGE_BASENAME).exists()
    retired = env / (db.LEGACY_USAGE_BASENAME + db.IMPORTED_SUFFIX)
    assert retired.exists() and "2026-09-01" in retired.read_text()


def test_an_existing_imported_file_is_never_overwritten(env):
    keep = env / (db.LEGACY_USAGE_BASENAME + db.IMPORTED_SUFFIX)
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("an earlier import's history\n")
    _write_jsonl(env / db.LEGACY_USAGE_BASENAME, [_usage_row("2026-09-02T10:00:00+00:00")])

    db.import_legacy_logs()

    assert keep.read_text() == "an earlier import's history\n"  # untouched
    assert len(list(env.glob(db.LEGACY_USAGE_BASENAME + db.IMPORTED_SUFFIX + "*"))) == 2


def test_corrupt_lines_are_skipped_and_the_rest_import(env):
    source = env / db.LEGACY_USAGE_BASENAME
    _write_jsonl(source, [_usage_row("2026-09-01T10:00:00+00:00")])
    with source.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write('{"timestamp": null, "profile_id": "p1"}\n')  # unusable key

    result = db.import_legacy_logs()

    assert result["usage_imported"] == 1 and result["usage_skipped"] == 1
    assert len(db.query("SELECT * FROM usage_event")) == 1


def test_a_degraded_store_imports_nothing_and_keeps_the_sources(env):
    db.close_this_thread()
    db.path().write_text("this is not a database")
    _write_jsonl(env / db.LEGACY_USAGE_BASENAME, [_usage_row("2026-09-01T10:00:00+00:00")])

    result = db.import_legacy_logs()

    assert result["ok"] is False and result["usage_imported"] == 0
    assert (env / db.LEGACY_USAGE_BASENAME).exists()  # history stays exactly where it was


def test_import_with_no_legacy_files_is_a_no_op(env):
    result = db.import_legacy_logs()
    assert result["ok"] and result["usage_imported"] == 0 and result["retired"] == []
