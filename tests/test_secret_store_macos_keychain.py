"""set_token must never leave a window with no credential present.

`add-generic-password -U` updates in place, so no separate delete call is
needed. Deleting first would destroy the stored credential whenever the add
half then failed (locked Keychain, disk full, permission denial).
"""

from unittest.mock import MagicMock, patch

from claude_unlimited.secret_store import macos_keychain


def test_set_token_calls_add_with_update_flag_only_no_delete_first():
    with patch("claude_unlimited.secret_store.macos_keychain.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        macos_keychain.set_token("profile-1", "secret-token")

    assert mock_run.call_count == 1  # exactly one subprocess call — no delete first
    args = mock_run.call_args.args[0]
    assert args[:2] == ["security", "add-generic-password"]
    assert "-U" in args
    assert "delete-generic-password" not in args


def test_set_token_does_not_swallow_a_real_add_failure():
    import subprocess

    with patch("claude_unlimited.secret_store.macos_keychain.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["security"])
        try:
            macos_keychain.set_token("profile-1", "secret-token")
            assert False, "expected CalledProcessError to propagate"
        except subprocess.CalledProcessError:
            pass

    # With no delete call, a failed add cannot destroy a pre-existing
    # credential; the single-call assertion above is what guarantees that.
