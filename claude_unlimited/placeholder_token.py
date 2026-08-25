"""The local-only credential `claude` authenticates to THIS daemon with.

Generated once and persisted: it is exported into the user's shell profile,
so it must stay stable across daemon restarts. It is never sent to Anthropic.
The Gateway compares it in constant time against what the client presents; a
mismatch is a straight 401 with no retry and no Rotation. The token proves
only that the request came from a process on this machine that went through
setup.
"""

from __future__ import annotations

import os
import secrets

from .config import APP_DIR, ensure_app_dir

TOKEN_FILE = APP_DIR / "placeholder_token"


def get_or_create() -> str:
    ensure_app_dir()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


def regenerate() -> str:
    token = secrets.token_urlsafe(32)
    ensure_app_dir()
    TOKEN_FILE.write_text(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


def matches(presented: str) -> bool:
    return secrets.compare_digest(presented, get_or_create())
