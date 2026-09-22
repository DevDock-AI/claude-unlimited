"""GET /api/branch-pins — which agent sits on which account."""
import json
import threading
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.profiles as profile_repo


class FakeSecretStore:
    def __init__(self): self.tokens = {}
    def set_token(self, pid, tok): self.tokens[pid] = tok
    def get_token(self, pid): return self.tokens[pid]
    def delete_token(self, pid): self.tokens.pop(pid, None)
    def has_token(self, pid): return pid in self.tokens


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
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown(); t.join(timeout=2)


def test_branch_pins_endpoint_answers(running_server):
    # Regression guard: the handler called _gateway() — a module-level
    # INSTANCE, not a factory — which raises TypeError only when a request
    # actually arrives. An import check cannot catch that.
    with urllib.request.urlopen(f"{running_server}/api/branch-pins", timeout=5) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert isinstance(body["pins"], list)


def test_branch_pins_name_their_profile_and_mark_subagents(running_server, monkeypatch):
    monkeypatch.setattr(daemon._gateway, "branch_pins", lambda: [
        {"session_id": "s1", "agent_id": "main", "parent_agent_id": None, "profile_id": "gone"},
        # The real shape of a DIRECT subagent: Claude Code sends its agent id
        # and no parent id. Keying the flag off the parent marked it "main".
        {"session_id": "s1", "agent_id": "a2", "parent_agent_id": None, "profile_id": "gone"},
        {"session_id": "s1", "agent_id": "a3", "parent_agent_id": "a2", "profile_id": "gone"},
    ])
    with urllib.request.urlopen(f"{running_server}/api/branch-pins", timeout=5) as r:
        pins = json.loads(r.read())["pins"]
    assert [p["is_subagent"] for p in pins] == [False, True, True]
    # A pin outliving its Profile must still be readable, not crash or vanish.
    assert all(p["profile_name"] == "deleted profile" for p in pins)
