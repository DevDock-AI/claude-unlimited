"""Linux implementation of the secret-store interface, via the Secret
Service D-Bus API — through the `secret-tool` CLI (part of libsecret,
usually packaged as `libsecret-tools` / `libsecret`), the same "shell out to
a system CLI" shape macos_keychain.py uses for `security`. See
docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is the
ONLY file that should ever shell out to `secret-tool` or know its shape.
Callers use claude_unlimited.secret_store, never this module directly.

UNVERIFIED: written to `secret-tool`'s documented command shape but not yet
exercised against a live Secret Service provider (GNOME Keyring, KWallet's
secret-service compat, etc.). See CONTRIBUTING.md's "third-OS smoke test"
guidance for what closes that gap.

Unlike macOS, where the Keychain is always present, a Secret Service provider
is not guaranteed to be running: a headless install with no desktop session
may have neither a D-Bus session bus nor a provider. Every function here
fails loudly with an actionable message in that case rather than falling back
to writing a credential to disk in plain text.
"""

from __future__ import annotations

import shutil
import subprocess

SERVICE = "claude-unlimited.oauth"


class SecretStoreError(RuntimeError):
    pass


def _require_secret_tool() -> None:
    if shutil.which("secret-tool") is None:
        raise SecretStoreError(
            "`secret-tool` was not found on PATH. Claude Unlimited stores credentials "
            "via the Secret Service API (libsecret) on Linux — install it with, e.g., "
            "`sudo apt install libsecret-tools` (Debian/Ubuntu) or `sudo dnf install "
            "libsecret` (Fedora), and make sure a provider (GNOME Keyring, KWallet, or "
            "similar) is running in your session."
        )


def set_token(profile_id: str, token: str) -> None:
    _require_secret_tool()
    result = subprocess.run(
        ["secret-tool", "store", "--label", f"Claude Unlimited: {profile_id}",
         "service", SERVICE, "account", profile_id],
        input=token, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SecretStoreError(
            f"secret-tool store failed for profile {profile_id!r}: {(result.stderr or result.stdout).strip()}"
        )


def get_token(profile_id: str) -> str:
    _require_secret_tool()
    result = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE, "account", profile_id],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise SecretStoreError(f"No stored credential found for profile {profile_id!r}.")
    # secret-tool lookup prints the secret with no trailing newline; strip
    # defensively, matching macos_keychain.get_token.
    return result.stdout.strip()


def delete_token(profile_id: str) -> None:
    if shutil.which("secret-tool") is None:
        return  # nothing to delete if the tool (and so the store) isn't even present
    subprocess.run(
        ["secret-tool", "clear", "service", SERVICE, "account", profile_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def has_token(profile_id: str) -> bool:
    try:
        return bool(get_token(profile_id))
    except Exception:
        return False
