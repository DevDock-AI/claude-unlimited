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

Threading: **one connection for the whole process**, shared across threads and
serialized by a lock. This module used to give each thread its own handle,
which looks right until you remember that the daemon serves a *thread per HTTP
connection*: every Dashboard poll, every widget tick and every proxied request
opened a new database handle that was never closed. Those handles pile up (76
were live on one real install), and in WAL mode each one holds a read mark,
so SQLite cannot checkpoint past the oldest of them — the write-ahead log grew
to 17 MB and every *new* handle then had to rebuild its index over that log
before its first read. The symptom was a Dashboard that took 10-20 seconds to
show any Profile, worst when the machine was busy.

A single connection cannot leak, keeps the WAL checkpointing normally, and
costs a lock around statements that take tens of milliseconds at most.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from . import config

DB_BASENAME = "claude_unlimited.db"
SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000

# The one connection, and the path it was opened for. Guarded by _open_lock.
_entry: Optional[tuple] = None
# Opening and migrating are serialized: the daemon starts several threads at
# once, and on a brand-new database they would otherwise all run the first
# migration concurrently — one wins, the rest hit "table already exists".
_open_lock = threading.RLock()
# Held around every statement, because the one connection is shared. Reentrant:
# import_legacy_logs() calls query() and execute() while already inside it.
_use_lock = threading.RLock()
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


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Per-project request counters. A plain GROUP BY over usage_event would
    undercount: project_usage counts every attributed request, including the
    ones that never yield a usage row (token counting, errors)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_request (
          project_id TEXT PRIMARY KEY,
          count INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """Which primitive speech level a reply was written under (speech.py), so
    its effect on output tokens is measured from real requests."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_event)")}
    if "speech_mode" not in columns:
        conn.execute("ALTER TABLE usage_event ADD COLUMN speech_mode TEXT")


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """Per-request Codex quota evidence: reasoning tokens (what ADR 0007 found
    drives the 5h window) and the 5h percentage the backend reported when the
    request started, so the window's movement can be attributed to requests."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_event)")}
    if "reasoning_tokens" not in columns:
        conn.execute("ALTER TABLE usage_event ADD COLUMN reasoning_tokens INTEGER")
    if "quota_5h_percent" not in columns:
        conn.execute("ALTER TABLE usage_event ADD COLUMN quota_5h_percent REAL")


_MIGRATIONS = [_migrate_to_1, _migrate_to_2, _migrate_to_3, _migrate_to_4]  # index 0 takes the schema from version 0 to 1


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
    """The process's connection, opened and migrated on first use. None when
    the store is unusable — callers treat that as "no storage", never as an
    error to raise.

    Shared across threads (see the module docstring), so every caller must run
    its statements under `_use_lock` — which `execute()` and `query()` do.
    """
    global _entry
    target = path()
    with _open_lock:
        cached = _entry
        if cached is not None and cached[0] == target:
            return cached[1]
        if cached is not None:
            # APP_DIR moved (the test suite does this per test). Close the old
            # handle rather than leaving it open on the previous file.
            _close_entry(cached[1])
            _entry = None
        if not available():
            return None
        try:
            config.ensure_app_dir()
            # check_same_thread=False: one connection, many threads, every
            # statement serialized by _use_lock.
            conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            _migrate(conn)
        except (sqlite3.Error, OSError) as exc:
            _degrade(target, exc)
            return None
        _entry = (target, conn)
        return conn


def _close_entry(conn: sqlite3.Connection) -> None:
    try:
        # TRUNCATE, not the implicit passive checkpoint on close: it is the one
        # that shrinks the write-ahead log back to nothing, so the next open
        # has no log to rebuild an index over.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    try:
        conn.close()
    except sqlite3.Error:
        pass


def execute(sql: str, params: Sequence[Any] = ()) -> Optional[int]:
    """One write, committed. Returns the new rowid, or None when the store is
    unusable — a lost row must never surface as an exception in the request
    path (see the module docstring)."""
    with _use_lock:
        conn = connect()
        if conn is None:
            return None
        try:
            with conn:
                return conn.execute(sql, params).lastrowid
        except (sqlite3.Error, OSError):
            # One statement failing is not evidence the store is unusable — a
            # lost row is dropped quietly, exactly like an unwritable activity
            # line. Only open/migrate failures degrade the database.
            return None


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _use_lock:
        conn = connect()
        if conn is None:
            return []
        try:
            return list(conn.execute(sql, params))
        except (sqlite3.Error, OSError):
            return []


def close_this_thread() -> None:
    """Closes the process's connection. Tests call it when they move
    `APP_DIR`; the daemon never needs it.

    Named for the per-thread handles this module used to keep — kept as the
    name every caller already uses, and still exactly "drop the handle".
    """
    global _entry
    with _open_lock:
        if _entry is not None:
            _close_entry(_entry[1])
            _entry = None
    _clear_degraded()


# ---- one-shot import of the JSONL logs this store replaces ----------------

LEGACY_USAGE_BASENAME = "usage_history.jsonl"
LEGACY_ACTIVITY_BASENAME = "activity.jsonl"
LEGACY_PROJECT_BASENAME = "project_usage.json"
IMPORTED_SUFFIX = ".imported"


def _json_lines(source: Path):
    """Every well-formed object in a JSONL file. A corrupt line is skipped, not
    fatal — the same rule the JSONL readers already followed."""
    try:
        raw = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _retire(source: Path) -> Optional[str]:
    """Rename an imported log aside. NEVER deletes, and never overwrites an
    existing `.imported` file — if one is there, this run's file keeps a
    distinct name so no history can be lost."""
    target = source.with_suffix(source.suffix + IMPORTED_SUFFIX)
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        target = source.with_suffix(f"{source.suffix}{IMPORTED_SUFFIX}.{stamp}")
    try:
        source.replace(target)
        return target.name
    except OSError:
        return None


def import_legacy_logs() -> dict:
    """Move `usage_history.jsonl` and `activity.jsonl` into the store, once.

    Idempotent by construction: a row already present (same timestamp, and the
    fields that identify the event) is skipped, so running this twice — or
    after a user restores a backup of the old file — cannot double-count.
    Sources are renamed aside only after their rows are in; a failure leaves
    them exactly where they were, because an upgrade must never be able to
    destroy history.
    """
    result = {"usage_imported": 0, "usage_skipped": 0,
              "activity_imported": 0, "activity_skipped": 0,
              "retired": [], "ok": True}
    if connect() is None:
        result["ok"] = False
        return result

    usage_source = config.APP_DIR / LEGACY_USAGE_BASENAME
    if usage_source.exists():
        seen = {(r["ts"], r["profile_id"], r["model"], r["input_tokens"], r["output_tokens"])
                for r in query("SELECT ts, profile_id, model, input_tokens, output_tokens FROM usage_event")}
        for row in _json_lines(usage_source):
            key = (row.get("timestamp"), row.get("profile_id"), row.get("model"),
                   _int(row.get("input_tokens")), _int(row.get("output_tokens")))
            if not key[0] or not key[1] or key in seen:
                result["usage_skipped"] += 1
                continue
            inserted = execute(
                """INSERT INTO usage_event (ts, profile_id, project_id, model, input_tokens, output_tokens,
                                            cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
                                            requested_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row.get("timestamp"), row.get("profile_id"), row.get("project_id"), row.get("model"),
                 _int(row.get("input_tokens")), _int(row.get("output_tokens")),
                 _int(row.get("cache_creation_input_tokens")), _int(row.get("cache_read_input_tokens")),
                 row.get("cost_usd"), row.get("requested_model")))
            if inserted is None:
                result["ok"] = False
                return result  # leave the source untouched
            seen.add(key)
            result["usage_imported"] += 1
        retired = _retire(usage_source)
        if retired:
            result["retired"].append(retired)

    project_source = config.APP_DIR / LEGACY_PROJECT_BASENAME
    if project_source.exists():
        try:
            counts = json.loads(project_source.read_text())
        except (OSError, json.JSONDecodeError):
            counts = {}
        if isinstance(counts, dict):
            for project_id, count in counts.items():
                if not isinstance(project_id, str):
                    continue
                # DO NOTHING on conflict: re-importing must not add to a counter
                # the daemon has been incrementing since the first import.
                execute("INSERT INTO project_request (project_id, count) VALUES (?, ?)"
                        " ON CONFLICT(project_id) DO NOTHING", (project_id, _int(count)))
            retired = _retire(project_source)
            if retired:
                result["retired"].append(retired)

    activity_source = config.APP_DIR / LEGACY_ACTIVITY_BASENAME
    if activity_source.exists():
        seen_activity = {(r["ts"], r["category"], r["text"], r["meta"])
                         for r in query("SELECT ts, category, text, meta FROM activity_event")}
        for row in _json_lines(activity_source):
            key = (row.get("timestamp"), row.get("category"), row.get("text"), row.get("meta"))
            if not key[0] or not key[1] or key in seen_activity:
                result["activity_skipped"] += 1
                continue
            inserted = execute("INSERT INTO activity_event (ts, category, text, meta) VALUES (?, ?, ?, ?)",
                               (row.get("timestamp"), row.get("category"), row.get("text", ""), row.get("meta")))
            if inserted is None:
                result["ok"] = False
                return result
            seen_activity.add(key)
            result["activity_imported"] += 1
        retired = _retire(activity_source)
        if retired:
            result["retired"].append(retired)

    return result
