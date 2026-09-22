"""The Dashboard's "reopen the HUD" button.

The button exists because the dock can be closed or quit, and there is
otherwise no way back to it without a terminal. The endpoints behind it are
deliberately narrow: one fixed local bundle, no caller-supplied input.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo


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


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_repo, "secret_store", FakeSecretStore())
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
    finally:
        server.shutdown()
        t.join(timeout=2)


def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _post(base, path, token=None):
    req = urllib.request.Request(f"{base}{path}", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("X-CSRF-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_widget_state_reports_support_installed_and_running(running_server, monkeypatch):
    base, _ = running_server
    status, body = _get(base, "/api/widget")
    assert status == 200
    # The Dashboard keys the button's visibility off these three, so all three
    # must always be present rather than omitted on the unsupported path.
    assert set(body) == {"supported", "installed", "running"}
    assert all(isinstance(v, bool) for v in body.values())


def test_widget_is_reported_unsupported_off_macos(running_server, monkeypatch):
    base, _ = running_server
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    status, body = _get(base, "/api/widget")
    assert status == 200
    assert body == {"supported": False, "installed": False, "running": False}


def test_launch_is_refused_off_macos(running_server, monkeypatch):
    base, token = running_server
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    status, body = _post(base, "/api/widget/launch", token)
    assert status == 400
    assert body["error"] == "unsupported"


def test_launch_is_refused_when_the_bundle_is_missing(running_server, monkeypatch, tmp_path):
    base, token = running_server
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    monkeypatch.setattr(daemon, "_widget_bundle", lambda: tmp_path / "nope.app")
    status, body = _post(base, "/api/widget/launch", token)
    assert status == 404
    assert body["error"] == "not_installed"


def test_launch_requires_csrf(running_server, monkeypatch):
    # State-changing and reachable from a browser, so it sits behind the same
    # gate as every other Dashboard POST.
    base, _ = running_server
    status, body = _post(base, "/api/widget/launch", token=None)
    assert status == 403
    assert body["error"] == "csrf"


def test_launch_opens_the_installed_bundle(running_server, monkeypatch, tmp_path):
    base, token = running_server
    bundle = tmp_path / "HUD - Heads-Up Display.app"
    bundle.mkdir()
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    monkeypatch.setattr(daemon, "_widget_bundle", lambda: bundle)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    status, body = _post(base, "/api/widget/launch", token)
    assert status == 200 and body == {"launched": True}
    # One fixed bundle path, nothing from the request.
    assert calls[-1] == ["open", "-a", str(bundle)]


def test_launch_failure_is_reported_not_raised(running_server, monkeypatch, tmp_path):
    base, token = running_server
    bundle = tmp_path / "HUD - Heads-Up Display.app"
    bundle.mkdir()
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    monkeypatch.setattr(daemon, "_widget_bundle", lambda: bundle)

    def boom(cmd, **kwargs):
        raise OSError("launch services is unavailable")

    monkeypatch.setattr(daemon.subprocess, "run", boom)
    status, body = _post(base, "/api/widget/launch", token)
    assert status == 500
    assert body["error"] == "launch_failed"


# ---- the widget's right-click "Take over" ----------------------------------
# The HUD is otherwise read-only; this is its one write. It has no
# page to read a <meta> CSRF token from, so it takes the token from
# /api/status and sends the same POST the Dashboard's kebab menu does.


def _widget_take_over(base, profile_id, token):
    # Exactly the request PoolClient.takeOver builds: no Origin, no cookie,
    # an empty body, the token from /api/status.
    req = urllib.request.Request(f"{base}/api/profiles/{profile_id}/take-over", data=b"", method="POST")
    if token is not None:
        req.add_header("X-CSRF-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("kind,extra", [
    ("oauth", {"account_uuid": "uuid-w"}),
    ("codex", {}),
    ("api", {}),
])
def test_widget_take_over_with_the_status_token_works_for_every_kind(running_server, kind, extra):
    base, _ = running_server
    profile_repo.create_profile(name="First", kind="api", credential="tok-long-enough-key")
    target = profile_repo.create_profile(name="Target", kind=kind, credential="tok-long-enough-key", **extra)
    _, status = _get(base, "/api/status")
    token = status["csrf_token"]
    assert token

    code, body = _widget_take_over(base, target.id, token)
    assert code == 200, body
    assert body["profile"]["id"] == target.id
    _, after = _get(base, "/api/status")
    assert after["current_profile_id"] == target.id


def test_widget_take_over_without_the_token_is_refused(running_server):
    base, _ = running_server
    target = profile_repo.create_profile(name="T", kind="api", credential="tok-long-enough-key")
    code, body = _widget_take_over(base, target.id, None)
    assert code == 403 and body["error"] == "csrf"


def test_widget_take_over_refuses_a_disabled_profile(running_server):
    # The widget greys the item out, but the daemon is the one that decides.
    base, token = running_server
    target = profile_repo.create_profile(name="T", kind="api", credential="tok-long-enough-key")
    profile_repo.update_profile(target.id, enabled=False)
    code, body = _widget_take_over(base, target.id, token)
    assert code == 400 and body["error"] == "disabled"


def test_widget_take_over_of_an_unknown_profile_is_404(running_server):
    base, token = running_server
    code, body = _widget_take_over(base, "nope", token)
    assert code == 404 and body["error"] == "not_found"


def test_the_bundle_is_the_renamed_hud_and_falls_back_to_the_old_name(monkeypatch, tmp_path):
    # Renamed from "Capacity Widget". Someone who has not rebuilt still has
    # only the old bundle, and the reopen button must keep working for them.
    monkeypatch.setattr(daemon.Path, "home", classmethod(lambda cls: tmp_path))
    apps = tmp_path / "Applications"
    apps.mkdir()
    assert daemon._widget_bundle() == apps / "HUD - Heads-Up Display.app"
    (apps / "CapacityWidget.app").mkdir()
    assert daemon._widget_bundle() == apps / "CapacityWidget.app"
    (apps / "HUD - Heads-Up Display.app").mkdir()
    assert daemon._widget_bundle() == apps / "HUD - Heads-Up Display.app"


def _stored(profile_id):
    """The profile as it is on disk, so persistence is checked rather than the
    response body echoing back what we sent."""
    return next(p for p in profile_repo.list_profiles() if p.id == profile_id)


def _widget_set_enabled(base, profile_id, enabled, token, raw=None):
    """Exactly the request PoolClient.setEnabled builds: a PATCH carrying a
    real JSON boolean and the token from /api/status, no Origin, no cookie."""
    payload = raw if raw is not None else json.dumps({"enabled": enabled}).encode()
    quoted = urllib.parse.quote(profile_id, safe="")
    req = urllib.request.Request(f"{base}/api/profiles/{quoted}", data=payload, method="PATCH")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("X-CSRF-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("kind,extra", [
    ("oauth", {"account_uuid": "uuid-e"}),
    ("codex", {}),
    ("api", {}),
])
def test_widget_enable_disable_works_for_every_kind(running_server, kind, extra):
    # The HUD's second write. `kind` is not patchable and nothing here touches
    # credentials, so the endpoint is the same for all three — but verifying
    # one kind verifies none, so all three are driven through it.
    base, _ = running_server
    target = profile_repo.create_profile(name="Target", kind=kind,
                                         credential="tok-long-enough-key", **extra)
    _, status = _get(base, "/api/status")
    token = status["csrf_token"]

    code, body = _widget_set_enabled(base, target.id, False, token)
    assert code == 200, body
    assert body["profile"]["enabled"] is False
    assert _stored(target.id).enabled is False
    # The kind survives a write that never mentions it.
    assert body["profile"]["kind"] == kind

    code, body = _widget_set_enabled(base, target.id, True, token)
    assert code == 200, body
    assert body["profile"]["enabled"] is True
    assert _stored(target.id).enabled is True


def test_widget_enable_disable_without_the_token_is_refused(running_server):
    base, _ = running_server
    target = profile_repo.create_profile(name="T", kind="api", credential="tok-long-enough-key")
    code, body = _widget_set_enabled(base, target.id, False, None)
    assert code == 403 and body["error"] == "csrf"
    assert _stored(target.id).enabled is True


def test_widget_enable_disable_wants_a_real_boolean(running_server):
    # The string "false" is a validation error, not a falsey value — which is
    # why PoolClient sends a JSON bool rather than interpolating a description.
    base, token = running_server
    target = profile_repo.create_profile(name="T", kind="api", credential="tok-long-enough-key")
    code, body = _widget_set_enabled(base, target.id, None, token,
                                     raw=json.dumps({"enabled": "false"}).encode())
    assert code == 400 and body["error"] == "validation"
    assert _stored(target.id).enabled is True


def test_widget_enable_disable_of_an_unknown_profile_is_404(running_server):
    base, token = running_server
    code, body = _widget_set_enabled(base, "nope", False, token)
    assert code == 404 and body["error"] == "not_found"
