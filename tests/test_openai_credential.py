import base64
import json
import time

from claude_unlimited.openai_credential import (
    StoredOpenAICredential,
    access_token_expires_at,
    chatgpt_email,
    chatgpt_plan_type,
    decode,
    decode_jwt_claims,
    encode,
    is_expiring_soon,
)


def _fake_jwt(claims: dict) -> str:
    """A syntactically valid JWT with a bogus signature.

    decode_jwt_claims never verifies signatures, so this is a faithful fixture
    without needing signing keys.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def test_decode_jwt_claims_reads_a_real_shaped_token():
    token = _fake_jwt({"exp": 12345, "email": "a@b.com"})
    assert decode_jwt_claims(token) == {"exp": 12345, "email": "a@b.com"}


def test_decode_jwt_claims_fails_open_on_garbage():
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims("a.b") == {}  # only 2 parts


def test_access_token_expires_at_reads_exp_claim():
    token = _fake_jwt({"exp": 1900000000})
    assert access_token_expires_at(token) == 1900000000.0


def test_access_token_expires_at_none_when_no_exp_claim():
    token = _fake_jwt({"email": "a@b.com"})
    assert access_token_expires_at(token) is None


def test_is_expiring_soon_true_within_the_buffer():
    now = 1_000_000.0
    token = _fake_jwt({"exp": now + 60})  # 60s out, well inside the 5-minute buffer
    assert is_expiring_soon(token, now=now) is True


def test_is_expiring_soon_false_well_before_expiry():
    now = 1_000_000.0
    token = _fake_jwt({"exp": now + 3600})
    assert is_expiring_soon(token, now=now) is False


def test_is_expiring_soon_false_when_expiry_unknown():
    # No exp claim at all: never force a refresh on a guess, matching
    # oauth_credential.py's is_expiring_soon.
    token = _fake_jwt({})
    assert is_expiring_soon(token) is False


def test_chatgpt_plan_type_reads_the_real_claim_shape():
    token = _fake_jwt({"chatgpt_plan_type": "plus"})
    assert chatgpt_plan_type(token) == "plus"


def test_chatgpt_plan_type_none_when_absent():
    token = _fake_jwt({"email": "a@b.com"})
    assert chatgpt_plan_type(token) is None


def test_chatgpt_email_reads_the_real_claim():
    token = _fake_jwt({"email": "user@example.com"})
    assert chatgpt_email(token) == "user@example.com"


def test_encode_decode_round_trip():
    cred = StoredOpenAICredential(access_token="tok-a", refresh_token="ref-a",
                                   account_id="acct-1", id_token="idtok")
    round_tripped = decode(encode(cred))
    assert round_tripped == cred


def test_decode_handles_missing_optional_fields():
    blob = json.dumps({"access_token": "tok-only"})
    cred = decode(blob)
    assert cred.access_token == "tok-only"
    assert cred.refresh_token is None
    assert cred.account_id is None
    assert cred.id_token is None


def test_decode_accepts_a_bare_api_key_string():
    # An api_key-mode codex Profile added through the standard Add Profile flow
    # goes through oauth_credential.encode() with no refresh_token/expires_at,
    # whose fallback stores a bare string rather than a JSON blob. decode()
    # must handle that.
    cred = decode("sk-proj-abc123def456")
    assert cred.access_token == "sk-proj-abc123def456"
    assert cred.refresh_token is None
    assert cred.account_id is None


def test_decode_accepts_a_bare_string_that_happens_to_start_with_brace_but_isnt_json():
    cred = decode("{not-valid-json-at-all")
    assert cred.access_token == "{not-valid-json-at-all"


def test_decode_accepts_valid_json_that_is_not_the_expected_shape():
    # A JSON array, or an object with no access_token key, still fails open to
    # treating the whole raw string as the token rather than raising.
    cred = decode(json.dumps({"something_else": "value"}))
    assert cred.access_token == json.dumps({"something_else": "value"})
