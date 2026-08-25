"""File-orchestration logic (path handling, error cases, the set/get round
trip) with `_protect`/`_unprotect` mocked out.

This does NOT verify the actual DPAPI encryption calls; see
windows_dpapi.py's module docstring.
"""

from unittest.mock import patch

import pytest

from claude_unlimited.secret_store import windows_dpapi as backend


@pytest.fixture(autouse=True)
def isolated_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "SECRETS_DIR", tmp_path / "secrets")
    return tmp_path


def _fake_protect(data: bytes) -> bytes:
    return b"ENC:" + data


def _fake_unprotect(data: bytes) -> bytes:
    assert data.startswith(b"ENC:")
    return data[len(b"ENC:"):]


def test_set_then_get_round_trips_through_the_encryption_calls():
    with patch.object(backend, "_protect", side_effect=_fake_protect), \
         patch.object(backend, "_unprotect", side_effect=_fake_unprotect):
        backend.set_token("profile-1", "secret-token")
        result = backend.get_token("profile-1")
    assert result == "secret-token"


def test_set_token_writes_through_protect_not_bare_plaintext():
    with patch.object(backend, "_protect", side_effect=_fake_protect) as mock_protect:
        backend.set_token("profile-1", "secret-token")
    mock_protect.assert_called_once_with(b"secret-token")
    raw = backend._path_for("profile-1").read_bytes()
    assert raw != b"secret-token"  # went through the (fake) encryption, not written bare
    assert raw == b"ENC:secret-token"


def test_get_token_raises_when_no_file_stored():
    with pytest.raises(backend.SecretStoreError):
        backend.get_token("never-stored")


def test_delete_token_removes_the_file():
    with patch.object(backend, "_protect", side_effect=_fake_protect):
        backend.set_token("profile-1", "secret-token")
    assert backend._path_for("profile-1").exists()
    backend.delete_token("profile-1")
    assert not backend._path_for("profile-1").exists()


def test_delete_token_is_a_noop_when_nothing_stored():
    backend.delete_token("never-stored")  # must not raise


def test_has_token_false_when_nothing_stored():
    assert backend.has_token("never-stored") is False


def test_has_token_true_after_set():
    with patch.object(backend, "_protect", side_effect=_fake_protect), \
         patch.object(backend, "_unprotect", side_effect=_fake_unprotect):
        backend.set_token("profile-1", "secret-token")
        assert backend.has_token("profile-1") is True
