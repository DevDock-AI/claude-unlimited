import json
import threading
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
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")

    server = daemon.make_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", daemon._CSRF_TOKEN
    finally:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def test_activity_empty_initially(running_server):
    base, _ = running_server
    with urllib.request.urlopen(f"{base}/api/activity", timeout=2) as resp:
        body = json.loads(resp.read())
    assert body["events"] == []


def test_activity_populated_after_creating_a_profile(running_server):
    base, token = running_server
    req = urllib.request.Request(
        f"{base}/api/profiles", method="POST",
        data=json.dumps({"name": "X", "kind": "oauth", "credential": "long-enough-token",
                          "account_uuid": "u1"}).encode(),
        headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=2)

    with urllib.request.urlopen(f"{base}/api/activity", timeout=2) as resp:
        body = json.loads(resp.read())
    assert len(body["events"]) == 1
    assert "added" in body["events"][0]["text"]
    assert body["events"][0]["category"] == "config"


def test_activity_limit_query_param_respected(running_server, monkeypatch):
    base, token = running_server
    import claude_unlimited.activity as activity
    for i in range(10):
        activity.record("config", f"event-{i}")

    with urllib.request.urlopen(f"{base}/api/activity?limit=3", timeout=2) as resp:
        body = json.loads(resp.read())
    assert len(body["events"]) == 3


def test_activity_since_until_query_params_respected(running_server):
    base, _ = running_server
    import claude_unlimited.activity as activity
    activity.ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with activity.ACTIVITY_FILE.open("a") as f:
        for day, text in [("18", "day-18"), ("19", "day-19"), ("20", "day-20")]:
            f.write(json.dumps({"timestamp": f"2026-08-{day}T00:00:00+00:00", "category": "config",
                                 "text": text, "meta": None}) + "\n")

    with urllib.request.urlopen(f"{base}/api/activity?since=2026-08-19T00:00:00%2B00:00", timeout=2) as resp:
        body = json.loads(resp.read())
    assert [e["text"] for e in body["events"]] == ["day-20", "day-19"]


def test_activity_export_downloads_full_json_with_disposition_header(running_server):
    base, _ = running_server
    import claude_unlimited.activity as activity
    activity.record("config", "an event")

    with urllib.request.urlopen(f"{base}/api/activity/export", timeout=2) as resp:
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        body = json.loads(resp.read())
    assert len(body["events"]) == 1
    assert body["events"][0]["text"] == "an event"
