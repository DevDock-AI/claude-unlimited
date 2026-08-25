"""macOS implementation of the secret-store interface, via the `security` CLI.

See docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is the
ONLY file that should ever shell out to `security` or know Keychain's shape.
Callers use claude_unlimited.secret_store, never this module directly.
"""

from __future__ import annotations

import subprocess

SERVICE_PREFIX = "claude-unlimited.oauth"


def _service(profile_id: str) -> str:
    return f"{SERVICE_PREFIX}.{profile_id}"


def set_token(profile_id: str, token: str) -> None:
    # `-U` updates the item in place if it already exists, so no delete step
    # is needed. Deleting first would risk losing the credential entirely if
    # the add then failed (locked Keychain, disk full, permission denial).
    service = _service(profile_id)
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", profile_id, "-s", service, "-w", token],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def get_token(profile_id: str) -> str:
    service = _service(profile_id)
    cp = subprocess.run(
        ["security", "find-generic-password", "-a", profile_id, "-s", service, "-w"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return cp.stdout.strip()


def delete_token(profile_id: str) -> None:
    service = _service(profile_id)
    subprocess.run(
        ["security", "delete-generic-password", "-a", profile_id, "-s", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def has_token(profile_id: str) -> bool:
    try:
        return bool(get_token(profile_id))
    except Exception:
        return False
