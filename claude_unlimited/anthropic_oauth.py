"""Everything needed to add an OAuth Profile without asking anyone to supply
an account UUID by hand.

Two things this module does:
  1. read_claude_code_credentials() backs the "Import current login" flow:
     it pulls Claude Code's own OAuth token from wherever this OS stores it,
     so nothing has to be copied by hand.
  2. fetch_account_profile() resolves any OAuth access token (imported, or
     pasted from `claude setup-token`) to its account_uuid by asking
     Anthropic directly, so the daemon looks the value up itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
MACOS_KEYCHAIN_SERVICE = "Claude Code-credentials"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


class CredentialImportError(RuntimeError):
    pass


class ProfileLookupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportedCredentials:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[int]
    subscription_type: Optional[str]


def _credentials_from_raw(raw: dict, source: str) -> ImportedCredentials:
    # Claude Code nests credentials under "claudeAiOauth".
    data = raw.get("claudeAiOauth", raw) if isinstance(raw, dict) else {}
    access_token = data.get("accessToken")
    if not access_token:
        raise CredentialImportError(f"Found a Claude Code credentials entry ({source}) but it has no accessToken.")

    return ImportedCredentials(
        access_token=access_token,
        refresh_token=data.get("refreshToken"),
        expires_at=data.get("expiresAt"),
        subscription_type=data.get("subscriptionType"),
    )


def _bundled_claude_cli() -> Optional[Path]:
    """The Claude Code CLI that ships INSIDE the Claude desktop app, if present.

    The app installs its own copy under its userData directory and puts nothing
    on PATH, so a machine can have a perfectly working CLI while `claude` is an
    unknown command - which is exactly the state someone is in when they reach
    this error from the desktop app."""

    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
        exe = "claude.exe"
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
        exe = "claude"
    else:
        base = Path.home() / ".config" / "Claude"
        exe = "claude"

    def version_key(path: Path):
        # Newest version wins; unparsable names sort last rather than crash.
        parts = []
        for chunk in path.parent.name.split("."):
            parts.append(int(chunk) if chunk.isdigit() else -1)
        return parts

    try:
        found = [p for p in (base / "claude-code").glob(f"*/{exe}") if p.is_file()]
    except OSError:
        return None
    return max(found, key=version_key) if found else None


def _installed_claude_cli() -> Optional[Path]:
    """The CLI put there by Claude Code's own installer, if PATH is stale.

    Its native installer writes to ~/.local/bin and adds that directory to
    PATH - but a terminal opened before that runs with the old environment, so
    the binary is present while `claude` is still an unknown command."""

    exe = "claude.exe" if platform.system() == "Windows" else "claude"
    candidate = Path.home() / ".local" / "bin" / exe
    return candidate if candidate.is_file() else None


def claude_cli_command() -> str:
    """How to invoke the Claude Code CLI on THIS machine, as runnable text.

    Ordered by how the person would want to run it: what PATH already resolves,
    then a real install PATH has not caught up with, and only then the copy
    bundled inside the desktop app (oldest and least expected)."""

    if shutil.which("claude"):
        return "claude"
    for finder in (_installed_claude_cli, _bundled_claude_cli):
        found = finder()
        if found is not None:
            # Quoted: these paths routinely contain spaces.
            return f'"{found}"'
    return ""


def no_default_login_message() -> str:
    """Why "Import current login" found nothing, in terms that fit THIS OS.

    This flow reads the session Claude Code itself stored, so it can only work
    once Claude Code has been signed in on this machine — that prerequisite is
    the first thing to say. Where the session lives is then platform-specific,
    and naming the macOS Keychain on Windows or Linux (as this message used to,
    on every OS) sends people hunting for something that does not exist there.
    See secret_store.BACKEND_NAME for the same per-platform honesty."""

    system = platform.system()
    if system == "Darwin":
        where = (
            f"neither {DEFAULT_CREDENTIALS_PATH} nor the login Keychain "
            f"(service {MACOS_KEYCHAIN_SERVICE!r}) holds one"
        )
    else:
        # Everywhere else Claude Code keeps the session in that file alone -
        # there is no Keychain equivalent to fall back on.
        where = f"{DEFAULT_CREDENTIALS_PATH} does not exist"

    platform_name = {"Darwin": "this Mac", "Windows": "this Windows PC",
                     "Linux": "this Linux machine"}.get(system, "this machine")

    cli = claude_cli_command()
    if cli == "claude":
        how = "Run `claude` in a terminal and complete `/login` (or run `claude setup-token`)."
    elif cli:
        how = (
            "The Claude desktop app ships the CLI but does not put it on PATH, so run it by "
            f"full path in a terminal - {cli} - and complete `/login`."
        )
    else:
        how = (
            "The Claude Code CLI is not installed on this machine - install it "
            "(https://docs.claude.com/en/docs/claude-code) and complete `/login` first."
        )

    return (
        f"No Claude Code login found on {platform_name} — {where}. "
        "This imports the login the Claude Code CLI saves for itself, so that CLI has to have "
        f"been signed in on this machine at least once. {how} "
        "Being signed into the Claude desktop app is NOT enough on its own - the app keeps its "
        "own session and writes no CLI credential. "
        "You can also paste a token manually instead."
    )


def read_claude_code_credentials(config_dir: Optional[Path] = None) -> ImportedCredentials:
    """Reads Claude Code's own logged-in OAuth session.

    With no `config_dir`, reads the default session. The file is tried first
    and Keychain second, because macOS Claude Code stores the credential in
    Keychain and writes no ~/.claude/.credentials.json at all.

    With `config_dir` given — an isolated `CLAUDE_CONFIG_DIR` a specific
    login run was pointed at, so it cannot disturb the normally logged-in
    account (see cli.py's add_account) — reads ONLY that login's credential:
    a `.credentials.json` inside the directory where one exists, else, on
    macOS, a Keychain entry under a directory-derived service name (see
    isolated_macos_keychain_service()).

    Deliberately does NOT fall back to the default location or the
    unsuffixed Keychain service: silently reading the shared session's
    credential would bind this Profile to an unrelated account with no sign
    anything went wrong."""

    if config_dir is not None:
        isolated_path = config_dir / ".credentials.json"
        if isolated_path.exists():
            try:
                raw = json.loads(isolated_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CredentialImportError(f"Could not read {isolated_path}: {exc}") from exc
            return _credentials_from_raw(raw, str(isolated_path))

        if platform.system() == "Darwin":
            service = isolated_macos_keychain_service(config_dir)
            raw = _read_macos_keychain_credentials(service=service)
            if raw is not None:
                return _credentials_from_raw(raw, f"macOS Keychain service {service!r}")
            checked = f"{isolated_path}, or macOS Keychain service {service!r}"
        else:
            checked = str(isolated_path)

        raise CredentialImportError(
            f"No credentials found for this isolated login — checked {checked}. This platform or "
            "Claude Code version may not isolate credentials via CLAUDE_CONFIG_DIR the way this "
            "command expects on this platform. Use \"Import current login\" in the Dashboard instead "
            "(sign into this account with plain `claude` first)."
        )

    raw = None
    if DEFAULT_CREDENTIALS_PATH.exists():
        try:
            raw = json.loads(DEFAULT_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialImportError(f"Could not read {DEFAULT_CREDENTIALS_PATH}: {exc}") from exc
    elif platform.system() == "Darwin":
        raw = _read_macos_keychain_credentials()

    if raw is None:
        raise CredentialImportError(no_default_login_message())

    return _credentials_from_raw(raw, "default session")


def remove_isolated_logins(config_dirs) -> int:
    """Removes the Keychain entries created by `add-account`'s isolated logins.

    Those are written by Claude Code itself, under a service name derived from
    the isolated directory we handed it, so they are ours to clean up and would
    otherwise outlive everything else — a live Anthropic refresh token sitting
    in the Keychain with nothing left referencing it.

    The derivation is what makes this safe: each name is
    "Claude Code-credentials-<hash of one of our own directories>". The user's
    real login is the un-suffixed "Claude Code-credentials", which no directory
    of ours can hash to, so it can never be selected here.

    Lives here rather than in cli.py because deleting a Profile has to do
    exactly this too, and cli.py is the layer above profiles.py — only `purge`
    used to clean these up, so removing an account from the Dashboard left its
    credential behind forever. Returns how many were removed."""
    if platform.system() != "Darwin" or not config_dirs:
        return 0
    removed = 0
    for config_dir in config_dirs:
        if not config_dir:
            continue
        service = isolated_macos_keychain_service(config_dir)
        if service == MACOS_KEYCHAIN_SERVICE:
            continue  # unreachable by construction; refuse anyway
        try:
            result = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True, timeout=10, check=False)
            if result.returncode == 0:
                removed += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return removed


def _read_macos_keychain_credentials(service: str = MACOS_KEYCHAIN_SERVICE) -> Optional[dict]:
    try:
        cp = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(cp.stdout.strip())
    except json.JSONDecodeError as exc:
        raise CredentialImportError(f"Keychain entry {service!r} was not valid JSON: {exc}") from exc


def isolated_macos_keychain_service(config_dir: Path) -> str:
    """The Keychain service name Claude Code uses for a login done under a
    custom CLAUDE_CONFIG_DIR.

    On macOS it writes no `.credentials.json` inside the isolated directory;
    the credential goes into a Keychain entry named
    f"{MACOS_KEYCHAIN_SERVICE}-{suffix}", where suffix is the first 8 hex
    characters of sha256(str(config_dir))."""
    suffix = hashlib.sha256(str(config_dir).encode("utf-8")).hexdigest()[:8]
    return f"{MACOS_KEYCHAIN_SERVICE}-{suffix}"


@dataclass(frozen=True)
class AccountProfile:
    account_uuid: str
    email: Optional[str]
    display_name: Optional[str]
    org_uuid: Optional[str]
    org_name: Optional[str]
    has_claude_max: Optional[bool]
    has_claude_pro: Optional[bool]


def plan_from_account(account: AccountProfile) -> Optional[str]:
    """The plan detected from Anthropic's /api/oauth/profile response.

    "max" takes priority, since an account can carry both flags. None means
    undetermined, never a guess. Every OAuth-adding path shares this one
    implementation — see profiles.upsert_oauth_profile()."""
    if account.has_claude_max:
        return "max"
    if account.has_claude_pro:
        return "pro"
    return None


def fetch_account_profile(access_token: str, timeout: float = 15.0) -> AccountProfile:
    """Resolves an OAuth access token to its account identity.

    A `claude setup-token` token cannot complete this lookup: it is scoped
    for headless inference only and gets a 403 (`permission_error`, "OAuth
    token does not meet scope requirement any_of(user:profile,
    user:office)"), no matter how often it is retried. `add-account` and
    "Import current login" go through Claude Code's full-scope login, which
    includes user:profile, and do work. The 403 is turned into a clearer
    message below."""

    req = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if exc.code == 403 and "scope requirement" in detail:
            raise ProfileLookupError(
                "This token doesn't have permission to look up account details — this happens with "
                "tokens from `claude setup-token`, which are scoped for headless use only. Use "
                "\"Import current login\" instead, or run `claude-unlimited add-account` from a terminal."
            ) from exc
        raise ProfileLookupError(f"Anthropic rejected this token (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProfileLookupError(f"Could not reach Anthropic to resolve this account: {exc}") from exc

    account = data.get("account") or {}
    org = data.get("organization") or {}
    account_uuid = account.get("uuid")
    if not account_uuid:
        raise ProfileLookupError("Anthropic's profile response had no account.uuid — unexpected response shape.")

    return AccountProfile(
        account_uuid=account_uuid,
        email=account.get("email"),
        display_name=account.get("display_name"),
        org_uuid=org.get("uuid"),
        org_name=org.get("name"),
        has_claude_max=account.get("has_claude_max"),
        has_claude_pro=account.get("has_claude_pro"),
    )
