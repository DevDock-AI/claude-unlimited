"""Secret-store interface: set_token/get_token/delete_token/has_token.

The single point that knows which OS backend to use. Every other module
imports THIS package, never a specific backend module — see
docs/adr/0002-pluggable-secret-store-and-daemon-installer.md. Supporting a
new OS means adding a backend module and one branch here, with no call site
changes.
"""

from __future__ import annotations

import platform

_SYSTEM = platform.system()

if _SYSTEM == "Darwin":
    from .macos_keychain import delete_token, get_token, has_token, set_token
elif _SYSTEM == "Linux":
    # UNVERIFIED — see linux_secretservice.py's module docstring.
    from .linux_secretservice import delete_token, get_token, has_token, set_token
elif _SYSTEM == "Windows":
    # UNVERIFIED — see windows_dpapi.py's module docstring.
    from .windows_dpapi import delete_token, get_token, has_token, set_token
else:
    raise RuntimeError(
        f"No secret-store backend for {_SYSTEM!r}. Claude Unlimited supports "
        "macOS, Linux, and Windows — see "
        "docs/adr/0002-pluggable-secret-store-and-daemon-installer.md for how "
        "to add one."
    )

__all__ = ["set_token", "get_token", "delete_token", "has_token"]
