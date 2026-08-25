from claude_unlimited.oauth_credential import StoredOAuthCredential, decode, encode, is_expiring_soon


def test_encode_returns_plain_string_when_nothing_extra_to_store():
    cred = StoredOAuthCredential(access_token="tok-abc")
    assert encode(cred) == "tok-abc"


def test_encode_decode_round_trip_with_refresh_token_and_expiry():
    cred = StoredOAuthCredential(access_token="tok-abc", refresh_token="ref-xyz", expires_at=1234567890000)
    stored = encode(cred)
    assert stored != "tok-abc"  # not stored as a plain string anymore
    decoded = decode(stored)
    assert decoded == cred


def test_decode_treats_a_legacy_plain_token_as_no_refresh_capability():
    decoded = decode("plain-legacy-access-token")
    assert decoded.access_token == "plain-legacy-access-token"
    assert decoded.refresh_token is None
    assert decoded.expires_at is None


def test_decode_falls_back_gracefully_on_malformed_json_looking_string():
    # Starts with "{" but isn't the expected JSON: must not raise, and must not
    # lose the string as a usable credential.
    decoded = decode("{not-actually-json")
    assert decoded.access_token == "{not-actually-json"


def test_is_expiring_soon_false_when_expiry_unknown():
    # Never force a refresh on a guess: a credential with no known expiry
    # (e.g. a manually pasted token) is left alone.
    cred = StoredOAuthCredential(access_token="tok", refresh_token="ref", expires_at=None)
    assert is_expiring_soon(cred) is False


def test_is_expiring_soon_true_once_inside_the_buffer_or_past():
    # Derived from the constant rather than hardcoded, since the buffer is tied
    # to the rate-limit backoff and has to be free to move.
    from claude_unlimited.oauth_credential import EXPIRING_SOON_BUFFER_MS

    now_ms = 1_000_000_000_000
    one_minute = 60 * 1000
    just_outside_buffer = StoredOAuthCredential(access_token="tok", refresh_token="ref",
                                                 expires_at=now_ms + EXPIRING_SOON_BUFFER_MS + one_minute)
    just_inside_buffer = StoredOAuthCredential(access_token="tok", refresh_token="ref",
                                                expires_at=now_ms + EXPIRING_SOON_BUFFER_MS - one_minute)
    already_past = StoredOAuthCredential(access_token="tok", refresh_token="ref", expires_at=now_ms - 1000)

    assert is_expiring_soon(just_outside_buffer, now_ms=now_ms) is False
    assert is_expiring_soon(just_inside_buffer, now_ms=now_ms) is True
    assert is_expiring_soon(already_past, now_ms=now_ms) is True


def test_refresh_buffer_outlasts_the_rate_limit_backoff():
    """The proactive-refresh buffer must exceed the post-429 backoff, or the
    token expires while refreshing is still blocked and the Profile needs a
    manual re-auth."""
    from claude_unlimited.gateway import Gateway
    from claude_unlimited import oauth_credential

    buffer_seconds = oauth_credential.EXPIRING_SOON_BUFFER_MS / 1000
    assert buffer_seconds > Gateway._RATE_LIMIT_BACKOFF_SECONDS, (
        f"refresh buffer {buffer_seconds}s must exceed the post-429 backoff "
        f"{Gateway._RATE_LIMIT_BACKOFF_SECONDS}s")
    assert buffer_seconds - Gateway._RATE_LIMIT_BACKOFF_SECONDS >= 120, "keep at least 2 min of margin"
