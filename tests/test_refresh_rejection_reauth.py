"""A refused refresh (invalid_grant and the like, issue #10) marks the account
needs-re-auth straight away — but only once its access token has expired. One
whose token still has time left keeps serving: marking a healthy account
"needs re-auth" is the mistake this must never make. Both refreshing kinds
(oauth, codex) are covered; an api Profile has no refresh at all."""

import base64
import json
import time

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import oauth_credential, oauth_login, openai_credential, openai_login
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.router import ProfileState


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "rs.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "uh.jsonl")
    monkeypatch.setattr(gateway_module.notifications, "notify_if_enabled", lambda *a, **k: None)
    for state in (bridge_module._refresh_not_before, bridge_module._refresh_rate_limited_streak,
                  bridge_module._refresh_failure_streak, bridge_module._refresh_rejected):
        state.clear()
    yield tmp_path
    for state in (bridge_module._refresh_not_before, bridge_module._refresh_rate_limited_streak,
                  bridge_module._refresh_failure_streak, bridge_module._refresh_rejected):
        state.clear()


def _state_after_sync(status_code, *, kind, expired):
    gw = Gateway(transport=lambda req: pytest.fail("no request is made"))
    gw.runtime_snapshot()
    gw.wait_for_credential_checks()
    return gw.runtime_snapshot()["a"].state


def _oauth(monkeypatch, status_code, expired):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", automatic=True, enabled=True)]))
    # Inside the expiring-soon buffer either way, so the refresh is attempted.
    expires_ms = (time.time() - 60) * 1000 if expired else (time.time() + 60) * 1000
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": oauth_credential.encode(
        oauth_credential.StoredOAuthCredential(access_token="tok", refresh_token="ref", expires_at=expires_ms))}))

    def refused(refresh_token, timeout=30.0):
        raise oauth_login.OAuthLoginError("invalid_grant", status_code=status_code)

    monkeypatch.setattr(gateway_module.oauth_login, "refresh_access_token", refused)


def _jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def _codex(monkeypatch, status_code, expired):
    save_pool(Pool(profiles=[Profile(id="a", name="C", kind="codex", auth_mode="chatgpt_subscription",
                                     automatic=True, enabled=True)]))
    exp = time.time() - 60 if expired else time.time() + 60
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": openai_credential.encode(
        openai_credential.StoredOpenAICredential(access_token=_jwt(exp), refresh_token="ref",
                                                 account_id="acct", id_token=None))}))

    def refused(refresh_token, timeout=30.0):
        raise openai_login.OpenAILoginError("invalid_grant", status_code=status_code)

    monkeypatch.setattr(bridge_module.openai_login, "refresh_access_token", refused)


@pytest.mark.parametrize("setup", [_oauth, _codex], ids=["oauth", "codex"])
def test_a_refused_refresh_on_an_expired_token_needs_reauth_now(env, monkeypatch, setup):
    setup(monkeypatch, 400, expired=True)
    assert _state_after_sync(400, kind=None, expired=True) == ProfileState.AUTH_INVALID


@pytest.mark.parametrize("setup", [_oauth, _codex], ids=["oauth", "codex"])
def test_a_refused_refresh_on_a_still_valid_token_keeps_serving(env, monkeypatch, setup):
    setup(monkeypatch, 400, expired=False)
    assert _state_after_sync(400, kind=None, expired=False) == ProfileState.ELIGIBLE


@pytest.mark.parametrize("setup", [_oauth, _codex], ids=["oauth", "codex"])
@pytest.mark.parametrize("status", [429, 503, None])
def test_a_throttled_or_transient_failure_never_marks_reauth(env, monkeypatch, setup, status):
    setup(monkeypatch, status, expired=True)
    assert _state_after_sync(status, kind=None, expired=True) == ProfileState.ELIGIBLE
