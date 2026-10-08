"""Three small routing behaviours that had only ever lived in a local install:

* a pool bigger than the minimum rotation bound gets one full pass;
* a paid API fallback hands traffic back to an available subscription at the
  next request boundary instead of waiting out the idle gate;
* the oauth credential check reads the keystore (a subprocess on macOS) at
  most once per cooldown, except right after a fresh rejection.
"""

import time
from datetime import datetime, timezone

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, Profile, Settings, load_pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.observation import AuthInvalid
from claude_unlimited.router import ProfileState
from claude_unlimited.upstream import UpstreamResponse

_OK = {"anthropic-ratelimit-unified-5h-utilization": "0.1",
       "anthropic-ratelimit-unified-5h-reset": "1787191800"}
_SPENT = {"anthropic-ratelimit-unified-5h-status": "rejected"}


class _Conn:
    def close(self):
        pass


def _resp(status, headers=None):
    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=iter([b"ok"]), connection=_Conn())


class _Secrets:
    def __init__(self):
        self.reads = []

    def get_token(self, profile_id):
        self.reads.append(profile_id)
        return "tok-" + profile_id


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    secrets = _Secrets()
    monkeypatch.setattr(gateway_module, "secret_store", secrets)
    return secrets


def _drain(result):
    list(result.body_chunks or ())
    return result


def test_a_pool_larger_than_the_minimum_bound_gets_one_full_pass(env):
    ids = [f"p{i}" for i in range(7)]
    save_pool(Pool(profiles=[Profile(id=i, name=i, kind="oauth", priority=n + 1, automatic=True, enabled=True)
                             for n, i in enumerate(ids)]))
    seen = []

    def transport(req):
        seen.append(req.headers.get("Authorization"))
        return _resp(200, _OK) if len(seen) == 7 else _resp(429, _SPENT)

    result = _drain(Gateway(transport=transport).handle("POST", "/v1/messages", {}, b"{}"))
    assert gateway_module.MAX_ROTATION_ATTEMPTS < 7
    assert result.status == 200 and result.profile_id == "p6"
    assert len(set(seen)) == 7  # every profile tried exactly once


def _api_then_subscription(**settings):
    return Pool(profiles=[
        Profile(id="sub", name="Sub", kind="oauth", priority=1, automatic=True, enabled=True),
        Profile(id="paid", name="Paid", kind="api", priority=2, automatic=True, enabled=True),
    ], settings=Settings(**settings))


def _fail_over_to_paid():
    calls = []

    def transport(req):
        calls.append(req.headers.get("Authorization"))
        if len(calls) == 1:
            return _resp(429, _SPENT)
        return _resp(200, _OK)

    return transport


def _recovered_gateway(**settings):
    save_pool(_api_then_subscription(**settings))
    gw = Gateway(transport=_fail_over_to_paid())
    assert _drain(gw.handle("POST", "/v1/messages", {}, b"{}")).profile_id == "paid"
    gw._runtime["sub"].state = ProfileState.ELIGIBLE  # the subscription window reset
    gw._runtime["sub"].resets_at = None
    return gw


def test_a_paid_fallback_hands_back_to_the_subscription_without_waiting_for_idle(env):
    gw = _recovered_gateway(return_to_preferred=True)
    # The last request was moments ago: the idle gate alone would keep us on "paid".
    assert _drain(gw.handle("POST", "/v1/messages", {}, b"{}")).profile_id == "sub"


def test_the_handback_is_still_opt_in(env):
    gw = _recovered_gateway()
    assert _drain(gw.handle("POST", "/v1/messages", {}, b"{}")).profile_id == "paid"


def test_a_pinned_request_never_moves_the_rotation_pointer(env):
    gw = _recovered_gateway(return_to_preferred=True)
    pinned = _drain(gw.handle("POST", "/v1/messages", {}, b"{}", forced_profile_id="paid"))
    assert pinned.profile_id == "paid"
    assert gw._current_profile_id == "paid"


# ---- credential check throttle --------------------------------------------

def _oauth_gateway(env):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True)]))
    gw = Gateway(transport=lambda req: None)
    gw._sync_snapshot(load_pool())
    env.reads.clear()
    return gw, load_pool().get("a")


def test_a_healthy_credential_is_read_from_the_keystore_once_per_cooldown(env):
    gw, profile = _oauth_gateway(env)
    rt = gw._runtime["a"]
    for _ in range(5):
        gw._maybe_check_oauth_credential(profile, rt)
    assert env.reads == ["a"]


def test_a_rejected_credential_is_not_held_back_by_the_healthy_check_cooldown(env):
    gw, profile = _oauth_gateway(env)
    gw._maybe_check_oauth_credential(profile, gw._runtime["a"])
    assert env.reads == ["a"]  # the healthy check just ran and claimed its cooldown

    with gw._lock:
        gw._observe("a", AuthInvalid(), datetime.now(timezone.utc))
    assert gw._runtime["a"].state == ProfileState.AUTH_INVALID
    gw._maybe_check_oauth_credential(profile, gw._runtime["a"])
    assert env.reads == ["a", "a"]  # only _refresh_attempt_due paces a rejected Profile
