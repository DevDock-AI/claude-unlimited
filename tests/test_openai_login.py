import io
import json

import claude_unlimited.openai_login as login_module
from claude_unlimited.openai_login import OpenAILoginError, refresh_access_token


class _FakeResponse(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeCompletedProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _fake_curl_run_factory(status: int, body: bytes, response_headers: bytes = b""):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["payload"] = json.loads(input)
        header_path = cmd[cmd.index("-D") + 1]
        with open(header_path, "wb") as f:
            f.write(f"HTTP/1.1 {status}\r\n".encode() + response_headers)
        return _FakeCompletedProcess(stdout=body + b"\n" + str(status).encode())

    return fake_run, captured


def test_refresh_access_token_sends_the_real_confirmed_payload_shape(monkeypatch):
    # client_id/grant_type/endpoint copied verbatim from
    # codex-rs/login/src/auth/manager.rs; see this module's docstring.
    fake_run, captured = _fake_curl_run_factory(200, json.dumps({
        "access_token": "tok-new", "refresh_token": "ref-new", "id_token": "idtok-new",
    }).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    tokens = refresh_access_token("ref-old")

    assert tokens.access_token == "tok-new"
    assert tokens.refresh_token == "ref-new"
    assert tokens.id_token == "idtok-new"
    assert captured["cmd"][4] == login_module.TOKEN_ENDPOINT
    assert login_module.TOKEN_ENDPOINT == "https://auth.openai.com/oauth/token"
    assert captured["payload"]["grant_type"] == "refresh_token"
    assert captured["payload"]["refresh_token"] == "ref-old"
    assert captured["payload"]["client_id"] == login_module.CLIENT_ID
    assert login_module.CLIENT_ID == "app_EMoamEEZ73f0CkXaXp7hrann"


def test_refresh_access_token_keeps_old_refresh_token_when_response_omits_one(monkeypatch):
    fake_run, _ = _fake_curl_run_factory(200, json.dumps({"access_token": "tok-new"}).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    tokens = refresh_access_token("ref-old")
    assert tokens.access_token == "tok-new"
    assert tokens.refresh_token is None  # caller's job to keep the old one, not this function's


def test_refresh_access_token_raises_with_status_code_on_4xx(monkeypatch):
    fake_run, _ = _fake_curl_run_factory(400, json.dumps({"error": "invalid_grant"}).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    try:
        refresh_access_token("dead-token")
        assert False, "expected OpenAILoginError"
    except OpenAILoginError as exc:
        assert exc.status_code == 400


def test_refresh_access_token_raises_with_429_status_code(monkeypatch):
    # openai_bridge._refresh_if_needed keys its long backoff on this status, so
    # it must survive the curl round-trip intact.
    fake_run, _ = _fake_curl_run_factory(429, json.dumps({"error": "rate_limit_error"}).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    try:
        refresh_access_token("ref-old")
        assert False, "expected OpenAILoginError"
    except OpenAILoginError as exc:
        assert exc.status_code == 429


def test_refresh_access_token_falls_back_to_urllib_when_curl_not_on_path(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data)
        return _FakeResponse(json.dumps({"access_token": "tok-via-urllib"}).encode())

    monkeypatch.setattr(login_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(login_module.urllib.request, "urlopen", fake_urlopen)

    tokens = refresh_access_token("ref-old")

    assert tokens.access_token == "tok-via-urllib"
    assert captured["url"] == login_module.TOKEN_ENDPOINT
    assert captured["payload"]["client_id"] == login_module.CLIENT_ID


def test_missing_access_token_in_response_raises():
    # A pure post-parse validation path, reachable by monkeypatching _post_json
    # directly, so no network mocking is needed.
    import claude_unlimited.openai_login as m

    def fake_post_json(url, payload, timeout, extra_headers=None):
        return 200, {}, json.dumps({"refresh_token": "ref-new"}).encode()

    orig = m._post_json
    m._post_json = fake_post_json
    try:
        try:
            refresh_access_token("ref-old")
            assert False, "expected OpenAILoginError"
        except OpenAILoginError as exc:
            assert "no access_token" in str(exc)
    finally:
        m._post_json = orig
