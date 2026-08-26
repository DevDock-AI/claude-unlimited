"""An oauth Profile's stored credential is a JSON blob, not a bearer token.

Sending it verbatim is garbage to Anthropic, which answers 401 —
indistinguishable from a dead account. This is what made "Test connection"
mark healthy Profiles as needing re-auth.
"""
import json

import pytest

import claude_unlimited.connection_test as ct
from claude_unlimited.config import Pool, Profile


def test_an_oauth_blob_is_decoded_to_a_bare_token(monkeypatch):
    blob = json.dumps({"access_token": "real-token", "refresh_token": "r", "expires_at": 0})
    profile = Profile(id="p1", name="A", kind="oauth", auth_mode="oauth", account_uuid="u1")
    monkeypatch.setattr(ct, "load_pool", lambda: Pool(profiles=[profile]))
    monkeypatch.setattr(ct.secret_store, "get_token", lambda pid: blob)

    sent = {}

    def fake_build(profile, credential, method, path, headers, body):
        sent["credential"] = credential
        raise ct.ConnectionTestError("stop here")

    monkeypatch.setattr(ct.proxy, "build_upstream_request", fake_build)
    ct._last_test_at.clear()
    with pytest.raises(ct.ConnectionTestError):
        ct.test_connection("p1")

    assert sent["credential"] == "real-token"
    assert "refresh_token" not in sent["credential"]


def test_a_caller_supplied_credential_wins(monkeypatch):
    # The daemon refreshes through the Gateway, which owns the single shared
    # per-Profile refresh backoff clock.
    blob = json.dumps({"access_token": "stale", "refresh_token": "r", "expires_at": 0})
    profile = Profile(id="p1", name="A", kind="oauth", auth_mode="oauth", account_uuid="u1")
    monkeypatch.setattr(ct, "load_pool", lambda: Pool(profiles=[profile]))
    monkeypatch.setattr(ct.secret_store, "get_token", lambda pid: blob)

    sent = {}

    def fake_build(profile, credential, method, path, headers, body):
        sent["credential"] = credential
        raise ct.ConnectionTestError("stop here")

    monkeypatch.setattr(ct.proxy, "build_upstream_request", fake_build)
    ct._last_test_at.clear()
    with pytest.raises(ct.ConnectionTestError):
        ct.test_connection("p1", credential="freshly-refreshed")

    assert sent["credential"] == "freshly-refreshed"
