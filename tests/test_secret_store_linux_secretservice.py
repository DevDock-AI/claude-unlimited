"""Command-shape tests only.

These lock in what commands get run; they do not exercise a real Secret
Service provider or prove `secret-tool`'s behavior. See
linux_secretservice.py's module docstring.
"""

from unittest.mock import MagicMock, patch

import pytest

from claude_unlimited.secret_store import linux_secretservice as backend


def _ok(stdout="", returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


def test_set_token_calls_secret_tool_store_with_token_on_stdin():
    with patch.object(backend.shutil, "which", return_value="/usr/bin/secret-tool"), \
         patch.object(backend.subprocess, "run", return_value=_ok()) as mock_run:
        backend.set_token("profile-1", "secret-token")

    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[:2] == ["secret-tool", "store"]
    assert "service" in cmd and "claude-unlimited.oauth" in cmd
    assert "account" in cmd and "profile-1" in cmd
    assert kwargs["input"] == "secret-token"


def test_get_token_calls_secret_tool_lookup_and_strips_output():
    with patch.object(backend.shutil, "which", return_value="/usr/bin/secret-tool"), \
         patch.object(backend.subprocess, "run", return_value=_ok(stdout="secret-token\n")) as mock_run:
        result = backend.get_token("profile-1")

    assert result == "secret-token"
    args = mock_run.call_args.args[0]
    assert args == ["secret-tool", "lookup", "service", "claude-unlimited.oauth", "account", "profile-1"]


def test_get_token_raises_when_lookup_fails():
    with patch.object(backend.shutil, "which", return_value="/usr/bin/secret-tool"), \
         patch.object(backend.subprocess, "run", return_value=_ok(stdout="", returncode=1)):
        with pytest.raises(backend.SecretStoreError):
            backend.get_token("profile-1")


def test_missing_secret_tool_raises_clear_error_not_a_crash():
    with patch.object(backend.shutil, "which", return_value=None):
        with pytest.raises(backend.SecretStoreError, match="secret-tool"):
            backend.set_token("profile-1", "secret-token")


def test_delete_token_is_a_noop_when_secret_tool_missing():
    with patch.object(backend.shutil, "which", return_value=None), \
         patch.object(backend.subprocess, "run") as mock_run:
        backend.delete_token("profile-1")  # must not raise
    mock_run.assert_not_called()


def test_has_token_false_on_any_failure():
    with patch.object(backend.shutil, "which", return_value="/usr/bin/secret-tool"), \
         patch.object(backend.subprocess, "run", return_value=_ok(returncode=1)):
        assert backend.has_token("profile-1") is False
