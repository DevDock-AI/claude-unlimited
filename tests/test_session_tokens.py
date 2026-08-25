from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.session_tokens as session_tokens


@pytest.fixture(autouse=True)
def isolated_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_tokens, "SESSION_TOKENS_FILE", tmp_path / "session_tokens.json")


def test_resolve_unknown_token_returns_none():
    assert session_tokens.resolve("nope") is None


def test_get_or_create_then_resolve_round_trips_the_profile_id():
    token = session_tokens.get_or_create("prof-a")
    assert session_tokens.resolve(token) == "prof-a"


def test_get_or_create_reuses_the_existing_token_for_the_same_profile():
    first = session_tokens.get_or_create("prof-a")
    second = session_tokens.get_or_create("prof-a")
    assert first == second


def test_get_or_create_mints_distinct_tokens_for_distinct_profiles():
    a = session_tokens.get_or_create("prof-a")
    b = session_tokens.get_or_create("prof-b")
    assert a != b
    assert session_tokens.resolve(a) == "prof-a"
    assert session_tokens.resolve(b) == "prof-b"


def test_expired_token_no_longer_resolves():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)
    assert session_tokens.resolve(token) is None


def test_get_or_create_after_expiry_mints_a_fresh_token_not_the_stale_one():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)

    fresh = session_tokens.get_or_create("prof-a")
    assert fresh != token
    assert session_tokens.resolve(fresh) == "prof-a"
