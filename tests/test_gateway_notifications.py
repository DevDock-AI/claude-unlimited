import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.notifications as notifications
from claude_unlimited.config import Pool, Profile, Settings, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.upstream import UpstreamResponse


class FakeConnection:
    def close(self):
        pass


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def fake_response(status, headers=None, body=b"ok"):
    def chunks():
        if body:
            yield body

    return UpstreamResponse(status=status, headers=headers or {}, body_chunks=chunks(), connection=FakeConnection())


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.usage_history.USAGE_HISTORY_FILE", tmp_path / "usage_history.jsonl")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({"a": "tok-a", "b": "tok-b"}))
    return tmp_path


@pytest.fixture
def notify_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "send_macos_notification", lambda title, message: calls.append((title, message)))
    return calls


def _all_notifications_settings():
    return Settings(notifications_enabled=True, notify_update_available=True, notify_approaching_threshold=True,
                     notify_rotated=True, notify_quota_reset=True, notify_needs_attention=True)


def test_rotation_fires_rotated_notification_when_enabled(pool_env, notify_calls):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True, switch_threshold=98.0),
    ], settings=_all_notifications_settings()))

    responses = iter([
        fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"}),  # A exhausts
        fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"}),
    ])
    gw = Gateway(transport=lambda req: next(responses))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 200
    assert result.profile_id == "b"

    # First request ever — no "previous" profile, so no rotated notification yet.
    assert not any("Rotated" in m for _, m in notify_calls)


def test_rotation_across_two_requests_fires_rotated_notification(pool_env, notify_calls):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True, switch_threshold=98.0),
    ], settings=_all_notifications_settings()))

    ok_a = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw = Gateway(transport=lambda req: ok_a)
    gw.handle("POST", "/v1/messages", {}, b"{}")  # first request establishes current = a

    responses = iter([
        fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"}),  # A exhausts on the second request
        fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"}),
    ])
    gw._transport = lambda req: next(responses)
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.profile_id == "b"
    assert any("Rotated A" in m and "B" in m for _, m in notify_calls)


def test_rotated_notification_suppressed_when_disabled(pool_env, notify_calls):
    settings = _all_notifications_settings()
    settings = Settings(**{**settings.__dict__, "notify_rotated": False})
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True, switch_threshold=98.0),
    ], settings=settings))

    ok = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw = Gateway(transport=lambda req: ok)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    responses = iter([fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected"}), ok])
    gw._transport = lambda req: next(responses)
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert not any("Rotated" in m for _, m in notify_calls)


def test_no_eligible_profile_after_previous_fires_needs_attention(pool_env, notify_calls):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
    ], settings=_all_notifications_settings()))

    ok = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw = Gateway(transport=lambda req: ok)
    gw.handle("POST", "/v1/messages", {}, b"{}")

    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=False, switch_threshold=98.0),
    ], settings=_all_notifications_settings()))
    result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 503
    assert any("No eligible Profile" in m for _, m in notify_calls)


def test_approaching_threshold_fires_once_per_crossing(pool_env, notify_calls):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=20.0),
    ], settings=_all_notifications_settings()))

    near = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.17", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw = Gateway(transport=lambda req: near)
    gw.handle("POST", "/v1/messages", {}, b"{}")
    gw.handle("POST", "/v1/messages", {}, b"{}")  # second request at the same usage — no duplicate warning

    approaching_calls = [m for _, m in notify_calls if "approaching" in m]
    assert len(approaching_calls) == 1


def test_approaching_threshold_suppressed_when_disabled(pool_env, notify_calls):
    settings = _all_notifications_settings()
    settings = Settings(**{**settings.__dict__, "notify_approaching_threshold": False})
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=20.0),
    ], settings=settings))

    near = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.17", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw = Gateway(transport=lambda req: near)
    gw.handle("POST", "/v1/messages", {}, b"{}")
    assert notify_calls == []


def test_quota_reset_fires_when_exhausted_profile_recovers(pool_env, notify_calls):
    import datetime

    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, automatic=True, enabled=True, switch_threshold=98.0),
        Profile(id="b", name="B", kind="oauth", priority=2, automatic=True, enabled=True, switch_threshold=98.0),
    ], settings=_all_notifications_settings()))

    past_reset = str(int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)).timestamp()))
    responses = iter([
        fake_response(429, {"anthropic-ratelimit-unified-5h-status": "rejected", "anthropic-ratelimit-unified-5h-reset": past_reset}),  # A exhausts, resets in the past
        fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"}),
    ])
    gw = Gateway(transport=lambda req: next(responses))
    gw.handle("POST", "/v1/messages", {}, b"{}")  # A exhausts (resets_at already in the past), B serves

    ok = fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1", "anthropic-ratelimit-unified-5h-reset": "1787191800"})
    gw._transport = lambda req: ok
    gw.handle("POST", "/v1/messages", {}, b"{}")  # A's cooldown/exhaustion window has already passed -> recovered
    assert any("quota reset" in m.lower() or "available again" in m.lower() for _, m in notify_calls)


def test_crossing_token_budget_fires_needs_attention_once(pool_env, notify_calls):
    # Crossing a token_threshold must notify exactly once, and it must happen
    # on the poll path rather than only on a request.
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      token_threshold=231)], settings=_all_notifications_settings()))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 200, "output_tokens": 66})  # 266 >= 231

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()  # the Dashboard's poll path (GET /api/profiles), not handle()
    gw.runtime_snapshot()  # a second poll must not re-fire

    budget_calls = [m for _, m in notify_calls if "token budget" in m]
    assert len(budget_calls) == 1
    assert "266" in budget_calls[0] and "231" in budget_calls[0]


def test_token_budget_notification_suppressed_when_needs_attention_disabled(pool_env, notify_calls):
    settings = _all_notifications_settings()
    settings = Settings(**{**settings.__dict__, "notify_needs_attention": False})
    import claude_unlimited.usage_history as usage_history
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind="api", priority=1, automatic=True, enabled=True,
                                      token_threshold=100)], settings=settings))
    usage_history.record("a", None, "claude-sonnet-5", {"input_tokens": 100, "output_tokens": 50})

    gw = Gateway(transport=lambda req: fake_response(200))
    gw.runtime_snapshot()
    assert notify_calls == []
