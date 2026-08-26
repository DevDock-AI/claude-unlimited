"""The local-only credential `claude` authenticates to THIS daemon with.

Generated once and persisted: it is exported into the user's shell profile,
so it must stay stable across daemon restarts. It is never sent to Anthropic.
The Gateway compares it in constant time against what the client presents; a
mismatch is a straight 401 with no retry and no Rotation. The token proves
only that the request came from a process on this machine that went through
setup.

It is shaped like an Anthropic API key (`sk-` prefix) on purpose. Clients and
any gateway sitting in front of this daemon validate the shape of what they
are handed — LiteLLM, for one, rejects a key that does not start with `sk-`
before it looks at anything else. The prefix carries no meaning here and adds
no secrecy; the entropy after it is what matters.
"""

from __future__ import annotations

import os
import secrets

from .config import APP_DIR, ensure_app_dir

TOKEN_FILE = APP_DIR / "placeholder_token"

# `sk-cu-` so it reads as an API key to anything that checks, while staying
# obviously not an Anthropic one to a human.
TOKEN_PREFIX = "sk-cu-"


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _write(token: str) -> str:
    ensure_app_dir()
    TOKEN_FILE.write_text(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


def get_or_create() -> str:
    ensure_app_dir()
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing.startswith(TOKEN_PREFIX):
            return existing
        # Upgrades an install made before the prefix existed. Rewriting it is
        # safe: the token is only ever compared against itself, and the shell
        # export is re-read from this file.
        return _write(TOKEN_PREFIX + existing)
    return _write(_new_token())


def regenerate() -> str:
    return _write(_new_token())


def matches(presented: str) -> bool:
    return secrets.compare_digest(presented, get_or_create())
