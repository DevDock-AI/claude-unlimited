"""Windows implementation of the secret-store interface, via DPAPI
(CryptProtectData/CryptUnprotectData in crypt32.dll) through `ctypes` —
built into every Windows install, so this needs no extra dependency, the
same "nothing to pip install" property macos_keychain.py and
linux_secretservice.py have. See
docs/adr/0002-pluggable-secret-store-and-daemon-installer.md — this is the
ONLY file that should ever touch DPAPI or know the on-disk blob shape.
Callers use claude_unlimited.secret_store, never this module directly.

UNVERIFIED: written to DPAPI's documented Win32 API contract (the standard
ctypes idiom for pywin32-free DPAPI access) but not yet exercised on Windows.
See CONTRIBUTING.md's "third-OS smoke test" guidance for what closes that gap.

DPAPI encrypts to a blob only the current Windows user account can decrypt,
the same security property Keychain and Secret Service give the other two
backends: CryptProtectData/CryptUnprotectData with no explicit entropy and
CRYPTPROTECT_LOCAL_MACHINE off (the default — per-user, not per-machine). The
encrypted blob is stored as one file per Profile under
~/.claude-unlimited/secrets/; plaintext never touches disk.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from pathlib import Path

SECRETS_DIR = Path.home() / ".claude-unlimited" / "secrets"


class SecretStoreError(RuntimeError):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _protect(data: bytes) -> bytes:
    blob_in = _to_blob(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "claude-unlimited", None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise SecretStoreError(f"DPAPI CryptProtectData failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    blob_in = _to_blob(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise SecretStoreError(f"DPAPI CryptUnprotectData failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _path_for(profile_id: str) -> Path:
    # profile_id is always a secrets.token_hex(8) value (profiles.py._new_id):
    # a fixed hex-only shape, never user-supplied text, so building a path
    # from it directly raises no sanitization or traversal concern.
    return SECRETS_DIR / f"{profile_id}.bin"


def set_token(profile_id: str, token: str) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    encrypted = _protect(token.encode("utf-8"))
    _path_for(profile_id).write_bytes(encrypted)


def get_token(profile_id: str) -> str:
    path = _path_for(profile_id)
    if not path.exists():
        raise SecretStoreError(f"No stored credential found for profile {profile_id!r}.")
    return _unprotect(path.read_bytes()).decode("utf-8")


def delete_token(profile_id: str) -> None:
    _path_for(profile_id).unlink(missing_ok=True)


def has_token(profile_id: str) -> bool:
    try:
        return bool(get_token(profile_id))
    except Exception:
        return False
