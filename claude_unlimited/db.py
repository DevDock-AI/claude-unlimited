"""The SQLite store behind statistics and activity (ECO phase 1).

Why this exists: `usage_history.jsonl` trimmed at 20,000 events and
`activity.jsonl` at 2,000, so both were silently discarding the user's own
history. A Stats interface measured in months cannot be built on a log that
forgets, and "keep everything" in JSONL means reinventing a database.

Three properties this module owes the rest of the daemon:

* **It can never take the daemon down.** Opening, migrating or writing can all
  fail (a corrupt file, a full disk, a read-only home). Any such failure puts
  the module in DEGRADED mode: `available()` goes False, reads return empty,
  writes no-op, and the caller carries on. Activity lines and usage rows are
  worth less than the request that was being served.
* **It follows `config.APP_DIR` at call time**, never at import. The daemon has
  one directory for its whole run, but the test suite swaps `APP_DIR` per test,
  and a connection cached across that swap would read another test's database.
  The cache is therefore keyed by the resolved path.
* **Schema changes are forward-only migrations** driven by `PRAGMA
  user_version`, applied once, in order. That is what makes an update safe:
  a newer build migrates, an older build sees a version it doesn't know and
  degrades rather than corrupting.

Threading: the daemon serves a thread per connection, and a `sqlite3`
connection is not safe to share across threads, so each thread gets its own
(WAL mode, so readers never block the writer).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from . import config

DB_BASENAME = "claude_unlimited.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

_local = threading.local()
# Opening and migrating are serialized: the daemon starts several threads at
# once, and on a brand-new database they would otherwise all run the first
# migration concurrently — one wins, the rest hit "table already exists".
_open_lock = threading.RLock()
# Degraded state is per database FILE, not per process. A poisoned file in one
# APP_DIR must not disable a perfectly good store in another (the test suite
# swaps APP_DIR constantly, and a global flag made one bad file disable the
# rest of the run).
_degraded: dict = {}
_degraded_lock = threading.Lock()


def path() -> Path:
    """Resolved at call time: see the module docstring on `config.APP_DIR`."""
    return config.APP_DIR / DB_BASENAME


def available() -> bool:
    """Whether the CURRENT `config.APP_DIR`'s database is usable."""
    with _degraded_lock:
        return path() not in _degraded


def degraded_reason() -> Optional[str]:
    """Why this database is unusable, for the Dashboard to show, or None."""
    with _degraded_lock:
        return _degraded.get(path())


def _degrade(target, exc: BaseException) -> None:
    with _degraded_lock:
        _degraded[target] = f"{type(exc).__name__}: {exc}"[:200]


def _clear_degraded() -> None:
    with _degraded_lock:
        _degraded.pop(path(), None)


# ---- migrations (forward-only; append, never edit a shipped one) ----------

def _migrate_to_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage_event (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          profile_id TEXT NOT NULL,
          project_id TEXT,
          model TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL,
          requested_model TEXT,
          eco_bytes_saved INTEGER,
          eco_tokens_saved INTEGER,
          eco_mode TEXT
        );
        CREATE INDEX IF NOT EXISTS usage_event_ts ON usage_event(ts);
        CREATE INDEX IF NOT EXISTS usage_event_profile_ts ON usage_event(profile_id, ts);
        CREATE INDEX IF NOT EXISTS usage_event_project_ts ON usage_event(project_id, ts);

        CREATE TABLE IF NOT EXISTS activity_event (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          category TEXT NOT NULL,
          text TEXT NOT NULL,
          meta TEXT
        );
        CREATE INDEX IF NOT EXISTS activity_event_ts ON activity_event(ts);
        """
    )


_MIGRATIONS = [_migrate_to_1]  # index 0 takes the schema from version 0 to 1


def _migrate(conn: sqlite3.Connection) -> None:
    """Caller holds `_open_lock`. The version is re-read here rather than
    passed in, so a thread that waited on the lock sees what the winner did."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        # Written by a newer build. Degrade rather than guess at its shape.
        raise sqlite3.DatabaseError(
            f"database schema v{version} is newer than this build understands (v{SCHEMA_VERSION})")
    for index in range(version, SCHEMA_VERSION):
        with conn:  # one transaction per migration
            _MIGRATIONS[index](conn)
            conn.execute(f"PRAGMA user_version = {index + 1}")


def connect() -> Optional[sqlite3.Connection]:
    """This thread's connection, opened and migrated on first use. None when
    the store is unusable — callers treat that as "no storage", never as an
    error to raise."""
    target = path()
    cached = getattr(_local, "entry", None)
    if cached is not None and cached[0] == target:
        return cached[1]
    if not available():
        return None
    with _open_lock:
        try:
            config.ensure_app_dir()
            conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            _migrate(conn)
        except (sqlite3.Error, OSError) as exc:
            _degrade(target, exc)
            return None
    _local.entry = (target, conn)
    return conn


def execute(sql: str, params: Sequence[Any] = ()) -> Optional[int]:
    """One write, committed. Returns the new rowid, or None when the store is
    unusable — a lost row must never surface as an exception in the request
    path (see the module docstring)."""
    conn = connect()
    if conn is None:
        return None
    try:
        with conn:
            return conn.execute(sql, params).lastrowid
    except (sqlite3.Error, OSError):
        # One statement failing is not evidence the store is unusable — a lost
        # row is dropped quietly, exactly like an unwritable activity line.
        # Only open/migrate failures degrade the database.
        return None


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    conn = connect()
    if conn is None:
        return []
    try:
        return list(conn.execute(sql, params))
    except (sqlite3.Error, OSError):
        return []


def close_this_thread() -> None:
    """Drops this thread's handle. Tests call it when they move `APP_DIR`;
    the daemon never needs it."""
    entry = getattr(_local, "entry", None)
    if entry is not None:
        try:
            entry[1].close()
        except sqlite3.Error:
            pass
        _local.entry = None
    _clear_degraded()
