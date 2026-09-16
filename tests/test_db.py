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


def test_each_thread_gets_a_working_connection(env):
    errors = []

    def writer(n):
        try:
            db.execute("INSERT INTO activity_event (ts, category, text) VALUES (?, ?, ?)",
                       (f"t{n}", "config", f"from thread {n}"))
        except Exception as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)
        finally:
            db.close_this_thread()

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(db.query("SELECT * FROM activity_event")) == 4


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
