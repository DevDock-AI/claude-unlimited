import io
import json

import pytest

import claude_unlimited.oauth_login as login_module
from claude_unlimited.oauth_login import (
    OAuthLoginError,
    refresh_access_token,
)


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
    """Builds a fake `subprocess.run` that mimics curl.

    It captures the request payload piped via stdin (matching
    `_post_json_via_curl`'s `--data-binary @-`), writes `response_headers` to
    the file curl's `-D` flag points at, and returns curl-shaped stdout: body,
    a newline, then the status code (matching `-w '\n%{http_code}'`).
    """
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["payload"] = json.loads(input)
        header_path = cmd[cmd.index("-D") + 1]
        with open(header_path, "wb") as f:
            f.write(f"HTTP/1.1 {status}\r\n".encode() + response_headers)
        return _FakeCompletedProcess(stdout=body + b"\n" + str(status).encode())

    return fake_run, captured


def test_refresh_access_token_sends_correct_payload_and_parses_response(monkeypatch):
    fake_run, captured = _fake_curl_run_factory(200, json.dumps({
        "access_token": "tok-new", "refresh_token": "ref-new", "expires_at": 999,
    }).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    tokens = refresh_access_token("ref-old")

    assert tokens.access_token == "tok-new"
    assert tokens.refresh_token == "ref-new"
    assert tokens.expires_at == 999000  # normalized seconds -> milliseconds
    assert login_module.TOKEN_ENDPOINT in captured["cmd"]  # URL present (index is argv-order-independent)
    assert captured["payload"]["grant_type"] == "refresh_token"
    assert captured["payload"]["refresh_token"] == "ref-old"
    assert captured["payload"]["client_id"] == login_module.CLIENT_ID
    # Mimic Claude Code's refresh so Anthropic's endpoint doesn't bucket us into a
    # throttled path: send the scope field, the axios User-Agent, and --compressed.
    assert captured["payload"]["scope"] == login_module.REFRESH_SCOPE
    assert "--compressed" in captured["cmd"]
    ua = [captured["cmd"][i + 1] for i, a in enumerate(captured["cmd"]) if a == "-H" and captured["cmd"][i + 1].lower().startswith("user-agent:")]
    assert ua and login_module.REFRESH_USER_AGENT in ua[0]


def test_refresh_access_token_keeps_old_refresh_token_when_response_omits_one(monkeypatch):
    fake_run, _ = _fake_curl_run_factory(200, json.dumps({"access_token": "tok-new"}).encode())
    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    tokens = refresh_access_token("ref-old")
    assert tokens.refresh_token == "ref-old"  # a static (non-rotating) refresh_token isn't lost


def test_refresh_access_token_does_not_retry_a_dead_refresh_token(monkeypatch):
    # A 4xx means the refresh_token itself is dead: retrying cannot help, and
    # the account needs a re-login.
    calls = []

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        calls.append(1)
        header_path = cmd[cmd.index("-D") + 1]
        with open(header_path, "wb") as f:
            f.write(b"HTTP/1.1 400\r\n")
        return _FakeCompletedProcess(stdout=b'{"error":"invalid_grant"}\n400')

    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)

    with pytest.raises(OAuthLoginError, match="Token refresh failed"):
        refresh_access_token("dead-refresh-token")
    assert len(calls) == 1  # no retry on a 4xx


def test_refresh_access_token_retries_on_5xx_then_succeeds(monkeypatch):
    calls = []

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        calls.append(1)
        header_path = cmd[cmd.index("-D") + 1]
        with open(header_path, "wb") as f:
            f.write(b"HTTP/1.1 200\r\n" if len(calls) > 1 else b"HTTP/1.1 503\r\n")
        if len(calls) == 1:
            return _FakeCompletedProcess(stdout=b'{"error":"unavailable"}\n503')
        return _FakeCompletedProcess(stdout=json.dumps({"access_token": "tok-new"}).encode() + b"\n200")

    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)
    monkeypatch.setattr(login_module.time, "sleep", lambda s: None)  # keep the test fast

    tokens = refresh_access_token("ref-old")
    assert tokens.access_token == "tok-new"
    assert len(calls) == 2


def test_refresh_access_token_raises_after_exhausting_retries(monkeypatch):
    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        header_path = cmd[cmd.index("-D") + 1]
        with open(header_path, "wb") as f:
            f.write(b"HTTP/1.1 503\r\n")
        return _FakeCompletedProcess(stdout=b'{"error":"unavailable"}\n503')

    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)
    monkeypatch.setattr(login_module.time, "sleep", lambda s: None)

    with pytest.raises(OAuthLoginError, match="Token refresh failed"):
        refresh_access_token("ref-old")


def test_refresh_access_token_falls_back_to_urllib_when_curl_not_on_path(monkeypatch):
    # A system without curl still works, just without the Cloudflare bypass.
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse(json.dumps({"access_token": "tok-via-urllib"}).encode())

    monkeypatch.setattr(login_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(login_module.urllib.request, "urlopen", fake_urlopen)

    tokens = refresh_access_token("ref-old")

    assert tokens.access_token == "tok-via-urllib"
    assert captured["url"] == login_module.TOKEN_ENDPOINT
    assert captured["body"]["grant_type"] == "refresh_token"


def test_refresh_access_token_raises_clear_error_when_curl_itself_fails(monkeypatch):
    # curl on PATH but unable to reach the host (network down, DNS failure).
    # Falling back to urllib is pointless — it is Cloudflare-blocked anyway —
    # and this must not be treated as a retryable 5xx.
    calls = []

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        calls.append(1)
        return _FakeCompletedProcess(stdout=b"", returncode=6, stderr=b"Could not resolve host")

    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)
    monkeypatch.setattr(login_module.time, "sleep", lambda s: None)

    with pytest.raises(OAuthLoginError, match="curl exit 6"):
        refresh_access_token("ref-old")
    assert len(calls) == 3  # a network-level failure is retried, unlike a 4xx


def test_refresh_access_token_raises_clear_error_when_curl_times_out(monkeypatch):
    import subprocess as real_subprocess

    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        raise real_subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(login_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(login_module.subprocess, "run", fake_run)
    monkeypatch.setattr(login_module.time, "sleep", lambda s: None)

    with pytest.raises(OAuthLoginError, match="Timed out"):
        refresh_access_token("ref-old")
