"""POST /api/models/refresh: the `code`-session-launch catalogue trigger.

CSRF-exempt by design (the CLI has no CSRF token to present, like
GET /api/session-token), non-blocking by contract: the response must come
back while the actual refresh — model_catalogue.refresh_if_allowed, which
is throttled and backed off internally — is still running in its own
thread. Hermetic: the refresh itself is always stubbed here; the throttle
and sha logic have their own tests in test_model_catalogue.py.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.model_catalogue as model_catalogue


@pytest.fixture
def running_server(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _post(url, headers=None):
    req = urllib.request.Request(url, data=b"", method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status, json.loads(resp.read())


def test_refresh_endpoint_triggers_a_background_refresh_and_returns_fast(running_server, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_refresh(*args, **kwargs):
        started.set()
        release.wait(5)  # holds the worker; must NOT hold the response
        return True

    monkeypatch.setattr(model_catalogue, "refresh_if_allowed", slow_refresh)
    try:
        # urlopen has a 2 s timeout; the stub blocks for 5 s. Getting a
        # response at all proves the endpoint doesn't wait on the refresh.
        status, body = _post(f"{running_server}/api/models/refresh")
        assert status == 202
        assert body == {"status": "scheduled"}
        assert started.wait(2), "the background refresh thread never ran"
    finally:
        release.set()


def test_refresh_endpoint_needs_no_csrf_token(running_server, monkeypatch):
    # The CLI can't present one; the handler must accept a bare local POST.
    monkeypatch.setattr(model_catalogue, "refresh_if_allowed", lambda *a, **kw: True)
    status, _ = _post(f"{running_server}/api/models/refresh")
    assert status == 202


def test_refresh_endpoint_still_rejects_a_bad_host_header(running_server, monkeypatch):
    monkeypatch.setattr(model_catalogue, "refresh_if_allowed", lambda *a, **kw: True)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"{running_server}/api/models/refresh", headers={"Host": "evil.example"})
    assert excinfo.value.code == 400


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status, json.loads(resp.read())


def test_model_map_endpoint_returns_the_wire_keys_the_dashboard_reads(running_server):
    # app.js consumes exactly these keys; a rename on either side would ship
    # silently without this contract test.
    status, body = _get(f"{running_server}/api/codex/model-map")
    assert status == 200
    for key in ("mapping", "selectable_models", "claude_selectable_models",
                "reasoning_efforts", "claude_efforts", "defaults"):
        assert key in body, key
    assert body["claude_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    # Every mapping row carries the fields the editable table binds to.
    assert body["mapping"], "default rows must never be empty"
    row = body["mapping"][0]
    for field in ("claude_model", "claude_label", "openai_model", "reasoning_effort",
                  "claude_effort", "default_model", "default_effort", "overridden"):
        assert field in row, field
    # claude_selectable_models are {id,label} objects; defaults are full rows.
    assert set(body["claude_selectable_models"][0]) >= {"id", "label"}
    assert set(body["defaults"][0]) >= {"claude_model", "model", "effort"}
