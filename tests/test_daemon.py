import json
import urllib.request

import pytest

from claude_unlimited.daemon import make_server


def test_refuses_to_bind_non_loopback_host():
    with pytest.raises(ValueError):
        make_server(host="0.0.0.0", port=0)


def test_accepts_loopback_host_and_binds_it():
    server = make_server(host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_health_endpoint_returns_ok_json():
    server = make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    try:
        import threading

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                assert resp.status == 200
                body = json.loads(resp.read())
                assert body["status"] == "ok"
                assert resp.headers.get("Cache-Control") == "no-store"
        finally:
            server.shutdown()
            t.join(timeout=2)
    finally:
        server.server_close()


def test_unknown_path_is_gated_not_leaked():
    """GET on an upstream API path must never reveal anything to an
    unauthenticated caller. 401 and 404 are both acceptable; a 200 with
    content is not."""
    server = make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    try:
        import threading

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/messages", timeout=2)
                assert False, "expected HTTPError for an unauthenticated upstream path"
            except urllib.error.HTTPError as e:
                assert e.code in (401, 404), e.code
        finally:
            server.shutdown()
            t.join(timeout=2)
    finally:
        server.server_close()


def test_an_immediate_auto_install_restarts_on_the_running_port(monkeypatch):
    """An auto_install that lands while the pool is idle must restart so the new
    code actually runs — and onto the port the daemon is really serving, not a
    hardcoded DEFAULT_PORT. Previously only the deferred path restarted."""
    from claude_unlimited import daemon
    from claude_unlimited import updater
    from claude_unlimited.config import Settings

    monkeypatch.setattr(daemon, "_RUNNING_PORT", 4399)
    monkeypatch.setattr(daemon._gateway, "is_idle", lambda *_a, **_k: True)
    monkeypatch.setattr(updater, "run_update_cycle",
                        lambda *a, **k: updater.UpdateOutcome(
                            release=updater.Release(version="9.9.9", tag="v9.9.9", commit_sha="a"*40, notes=""),
                            action="installed"))
    monkeypatch.setattr(daemon, "_record_update_outcome", lambda *a, **k: None)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_for_update", lambda port: restarted.append(port))

    daemon._run_update_check(settings=Settings(update_mode="auto_install"), respect_idle=True)
    assert restarted == [4399]


def test_a_manual_check_never_installs_or_restarts(monkeypatch):
    from claude_unlimited import daemon
    from claude_unlimited import updater
    from claude_unlimited.config import Settings

    monkeypatch.setattr(updater, "run_update_cycle",
                        lambda *a, **k: updater.UpdateOutcome(
                            release=updater.Release(version="9.9.9", tag="v9.9.9", commit_sha="a"*40, notes=""),
                            action="available"))
    monkeypatch.setattr(daemon, "_record_update_outcome", lambda *a, **k: None)
    restarted = []
    monkeypatch.setattr(daemon, "_restart_for_update", lambda port: restarted.append(port))

    daemon._run_update_check(settings=Settings(update_mode="manual"), respect_idle=True, mode_override="manual")
    assert restarted == []
