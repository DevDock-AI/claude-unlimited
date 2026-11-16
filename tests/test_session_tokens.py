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


def test_resolve_grant_of_unknown_token_is_empty():
    grant = session_tokens.resolve_grant("nope")
    assert grant.forced_profile_id is None
    assert grant.distribute is False


def test_distribute_token_grants_distribute_and_no_pin():
    token = session_tokens.get_or_create_distribute()
    grant = session_tokens.resolve_grant(token)
    assert grant.distribute is True
    assert grant.forced_profile_id is None
    # The pin-only view of the same token must not invent a profile.
    assert session_tokens.resolve(token) is None


def test_get_or_create_distribute_reuses_its_token():
    assert session_tokens.get_or_create_distribute() == session_tokens.get_or_create_distribute()


def test_distribute_and_pinned_tokens_are_distinct():
    pinned = session_tokens.get_or_create("prof-a")
    distributed = session_tokens.get_or_create_distribute()
    assert pinned != distributed
    assert session_tokens.resolve_grant(pinned) == session_tokens.SessionGrant(forced_profile_id="prof-a")
    assert session_tokens.resolve_grant(distributed) == session_tokens.SessionGrant(distribute=True)


def test_get_or_create_distribute_does_not_hijack_a_pinned_entry():
    session_tokens.get_or_create("prof-a")
    token = session_tokens.get_or_create_distribute()
    assert session_tokens._load()[token].get("profile_id") is None


def test_a_pin_wins_over_distribute_if_an_entry_ever_carries_both():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    data[token]["mode"] = "distribute"
    session_tokens._save(data)

    grant = session_tokens.resolve_grant(token)
    assert grant.forced_profile_id == "prof-a"
    assert grant.distribute is False


def test_expired_distribute_token_no_longer_grants_anything():
    token = session_tokens.get_or_create_distribute()
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    session_tokens._save(data)
    assert session_tokens.resolve_grant(token) == session_tokens.SessionGrant()


def _age(token, days):
    data = session_tokens._load()
    data[token]["created_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data[token].pop("last_used_at", None)
    session_tokens._save(data)


def test_a_token_in_use_outlives_the_ttl_counted_from_creation():
    # A session relaunched onto a reused token, or simply left running,
    # must not be cut off 30 days after the token was first minted.
    token = session_tokens.get_or_create_distribute()
    _age(token, 29)
    assert session_tokens.resolve_grant(token).distribute
    data = session_tokens._load()
    data[token]["created_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    session_tokens._save(data)
    assert session_tokens.resolve_grant(token).distribute


def test_reusing_a_token_restarts_its_expiry_clock():
    token = session_tokens.get_or_create("prof-a")
    _age(token, 29)
    assert session_tokens.get_or_create("prof-a") == token
    data = session_tokens._load()
    data[token]["created_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    session_tokens._save(data)
    assert session_tokens.resolve(token) == "prof-a"


def test_a_recently_used_token_is_not_rewritten_on_every_request():
    token = session_tokens.get_or_create("prof-a")
    session_tokens.resolve(token)
    before = session_tokens.SESSION_TOKENS_FILE.stat().st_mtime_ns
    session_tokens.resolve(token)
    assert session_tokens.SESSION_TOKENS_FILE.stat().st_mtime_ns == before


def test_an_unused_token_still_expires():
    token = session_tokens.get_or_create("prof-a")
    data = session_tokens._load()
    stale = (datetime.now(timezone.utc) - session_tokens.SESSION_TOKEN_TTL - timedelta(days=1)).isoformat()
    data[token]["created_at"] = stale
    data[token]["last_used_at"] = stale
    session_tokens._save(data)
    assert session_tokens.resolve(token) is None
