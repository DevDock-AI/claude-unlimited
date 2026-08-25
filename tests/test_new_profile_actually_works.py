"""A freshly created Profile must be immediately selectable by the Router.

These go through the exact path every add-profile flow uses: create_profile
with no explicit `automatic`. If a new Profile isn't routable, nothing works
the moment someone adds their first account.
"""

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.profiles as profile_repo
from claude_unlimited.gateway import Gateway
from claude_unlimited.upstream import UpstreamResponse


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


class FakeConnection:
    def close(self):
        pass


def fake_response(status=200):
    def chunks():
        yield b'{"ok":true}'

    return UpstreamResponse(status=status, headers={"anthropic-ratelimit-unified-5h-utilization": "0.4",
                                                      "anthropic-ratelimit-unified-5h-reset": "1787191800"},
                             body_chunks=chunks(), connection=FakeConnection())


def test_a_freshly_created_profile_is_immediately_usable(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(gateway_module, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")

    # Exactly what every add-profile flow does: no `automatic=` argument.
    profile = profile_repo.create_profile(
        name="Personal Max", kind="oauth", credential="real-looking-token-123", account_uuid="acct-1",
    )
    assert profile.automatic is True, "a freshly created Profile must default to automatic=True"

    gw = Gateway(transport=lambda req: fake_response(200))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")

    assert result.status == 200, (
        "a brand-new Profile with no other configuration must be selectable on the very "
        "first request — this exact scenario returned 503 in production before the fix"
    )
    assert result.profile_id == profile.id
