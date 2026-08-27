"""Profile repository: the only place that coordinates config.json + Keychain.

Callers must never be able to create a half-saved Profile (metadata written
but no credential, or the reverse). This module owns that coordination and
rolls back on partial failure. Nothing here makes a network call, so it is
pure local state management and runs without the proxy.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from . import activity, connectors, oauth_credential, secret_store
from . import config as config_module
from .config import CONFIG_LOCK, Pool, Profile, load_pool, save_pool

# Derived from connectors.py, so a kind's name is registered in exactly one
# place. Both stay flat tuples shared across kinds; the per-kind auth_mode
# check lives in _validate() below.
_VALID_KINDS = tuple(connectors.CONNECTORS)
_VALID_AUTH_MODES = connectors.all_auth_modes()
# Must stay in sync with static/app.js's TAG_COLORS: that is the picker the
# Dashboard shows and this is what accepts or rejects what it sends. A color
# in only one of the two either can't be picked or is rejected on save.
_TAG_COLORS = ("#43C6FF", "#35D07F", "#FFB020", "#C97BFF", "#FF5FA6", "#2FD9C4", "#FF5C5C", "#FFD93D", "#6C8EFF", "#5C5C63")


class ValidationError(ValueError):
    pass


class ProfileRepositoryError(RuntimeError):
    pass


def _new_id() -> str:
    return secrets.token_hex(8)


def _validate(name: str, kind: str, base_url: Optional[str], auth_mode: str, tag_color: Optional[str]) -> None:
    if not name or not name.strip():
        raise ValidationError("Profile name is required.")
    if len(name) > 80:
        raise ValidationError("Profile name is too long (max 80 characters).")
    if kind not in _VALID_KINDS:
        raise ValidationError(f"Unknown Profile kind {kind!r}; must be one of {_VALID_KINDS}.")
    if kind in ("api", "codex") and auth_mode not in _VALID_AUTH_MODES:
        raise ValidationError(f"Unknown auth_mode {auth_mode!r}; must be one of {_VALID_AUTH_MODES}.")
    if base_url:
        if not re.match(r"^https://[^\s]+$", base_url):
            raise ValidationError(
                "base_url must start with https:// (loopback http:// is not accepted here; "
                "this validates the UPSTREAM target, a different trust boundary than the "
                "daemon's own local listener)."
            )
    if tag_color is not None and tag_color not in _TAG_COLORS:
        raise ValidationError(f"Unknown tag_color; must be one of {_TAG_COLORS}.")


# Directories a Profile is allowed to name. Both are created by cli.py under
# this tool's own app directory, and delete_profile() removes codex_home
# outright — so an unconstrained value here is a request to recursively
# delete a directory of someone's choosing. The roots come from config.py so
# the code that CREATES them and the code that VALIDATES them cannot drift.
def _validate_isolated_dir(value, field: str, root) -> None:
    from pathlib import Path
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty path, or null.")
    # resolve() both sides: a symlink or a ".." segment must not be able to
    # escape the root, and the root itself may be a symlink on some setups.
    try:
        candidate = Path(value).expanduser().resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"{field} is not a usable path: {exc}") from None
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValidationError(f"{field} must be inside {root}.")


def _validate_field_types(**changes) -> None:
    """Type- and range-checks every field a caller may set.

    This is not defensive nicety. `config.py`'s load_pool() coerces on read —
    `int(p["priority"])`, `float(p["switch_threshold"])` — so a value that is
    merely SAVED without checking becomes an exception on the next load, and
    load_pool() is called by every API handler, the proxy path, and the very
    request that would fix it. One bad PATCH could leave the daemon unable to
    start until someone hand-edited config.json.
    """
    from .openai_models import VALID_REASONING_EFFORTS

    def _num(key, *, kind, minimum=None, maximum=None, allow_none=False):
        if key not in changes:
            return
        value = changes[key]
        if value is None:
            if allow_none:
                return
            raise ValidationError(f"{key} cannot be null.")
        # bool is an int subclass; accepting True as a priority of 1 would be
        # a silent, confusing success.
        if isinstance(value, bool) or not isinstance(value, kind):
            wanted = ("a number" if isinstance(kind, tuple)
                      else {"int": "a whole number", "str": "text"}.get(kind.__name__, kind.__name__))
            raise ValidationError(f"{key} must be {wanted}, got {type(value).__name__}.")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{key} must be at least {minimum}.")
        if maximum is not None and value > maximum:
            raise ValidationError(f"{key} must be at most {maximum}.")

    _num("priority", kind=int, minimum=1)
    _num("switch_threshold", kind=(int, float), minimum=0, maximum=100)
    _num("token_threshold", kind=int, minimum=0, allow_none=True)
    _num("monthly_budget_cap", kind=(int, float), minimum=0, allow_none=True)

    for key in ("enabled", "automatic"):
        if key in changes and not isinstance(changes[key], bool):
            raise ValidationError(f"{key} must be true or false.")

    for key in ("name", "kind", "auth_mode"):
        if key in changes and not isinstance(changes[key], str):
            raise ValidationError(f"{key} must be a string.")

    for key in ("base_url", "default_model", "tag_color", "plan", "codex_model"):
        if key in changes and changes[key] is not None and not isinstance(changes[key], str):
            raise ValidationError(f"{key} must be a string, or null.")

    effort = changes.get("codex_reasoning_effort")
    if effort is not None and effort not in VALID_REASONING_EFFORTS:
        raise ValidationError(
            f"codex_reasoning_effort must be one of {list(VALID_REASONING_EFFORTS)}.")

    claude_root, codex_root = config_module.accounts_roots()
    _validate_isolated_dir(changes.get("claude_config_dir"), "claude_config_dir", claude_root)
    _validate_isolated_dir(changes.get("codex_home"), "codex_home", codex_root)


def list_profiles() -> list[Profile]:
    return load_pool().profiles


def find_by_account_uuid(account_uuid: str) -> Optional[Profile]:
    """Dedup key for OAuth Profiles: re-importing or re-running
    `add-account` for an already-added account must update its credential
    rather than create a duplicate row."""
    return next((p for p in load_pool().profiles if p.account_uuid == account_uuid), None)


def update_credential(profile_id: str, credential: str, *, refresh_token: Optional[str] = None,
                       expires_at: Optional[int] = None) -> None:
    if not credential or len(credential.strip()) < 8:
        raise ValidationError("Credential looks too short — paste the complete token/key.")

    # refresh_token/expires_at are passed only for an OAuth credential that
    # came with them (CLI `add-account`, "Import current login"). A manually
    # pasted token or API key leaves both None, and encode() then stores a
    # plain string.
    stored = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
        access_token=credential, refresh_token=refresh_token, expires_at=expires_at))
    secret_store.set_token(profile_id, stored)

    # Stamp credential_updated_at so the running daemon's Gateway — separate
    # in-memory state, possibly in another process — notices the fresh
    # credential and clears a stuck AUTH_INVALID state. recover_expired_
    # cooldowns only handles time-based recovery, not this.
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        name = existing.name if existing else profile_id
        if existing is not None:
            updated = replace(existing, credential_updated_at=datetime.now(timezone.utc).isoformat())
            pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
            save_pool(pool)

    activity.record("config", f"{name} credential refreshed")


def _stamp_credential_updated_at(profile_id: str) -> None:
    """Shared by update_credential() and update_credential_raw(): both need
    the same Profile.credential_updated_at signal that gateway.py's
    _sync_snapshot reads, whichever module owns the encoding."""
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is not None:
            updated = replace(existing, credential_updated_at=datetime.now(timezone.utc).isoformat())
            pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
            save_pool(pool)


def update_credential_raw(profile_id: str, encoded_blob: str) -> None:
    """Stores an ALREADY-encoded credential blob as-is, for a kind whose own
    module owns the encoding (openai_credential.py for a codex Profile).
    update_credential() instead re-encodes through oauth_credential.py's
    Anthropic-specific shape.

    Skips update_credential()'s length check: a JSON blob's length says
    nothing about the credential inside it."""
    secret_store.set_token(profile_id, encoded_blob)
    _stamp_credential_updated_at(profile_id)


def create_profile(
    *,
    name: str,
    kind: str,
    credential: str,
    base_url: Optional[str] = None,
    auth_mode: str = "api_key",
    priority: Optional[int] = None,
    switch_threshold: float = 98.0,
    # Defaults to True because no add-profile flow exposes a way to turn it
    # on, so a False default would leave a new Profile permanently invisible
    # to Rotation. Pass False only for a deliberate manual-pin-only account.
    automatic: bool = True,
    default_model: Optional[str] = None,
    monthly_budget_cap: Optional[float] = None,
    token_threshold: Optional[int] = None,
    tag_color: Optional[str] = None,
    account_uuid: Optional[str] = None,
    plan: Optional[str] = None,
    refresh_token: Optional[str] = None,
    expires_at: Optional[int] = None,
    claude_config_dir: Optional[str] = None,
    codex_home: Optional[str] = None,
    codex_model: Optional[str] = None,
    codex_reasoning_effort: Optional[str] = None,
    credential_already_encoded: bool = False,
) -> Profile:
    _validate(name, kind, base_url, auth_mode, tag_color)
    # Same reachable-from-HTTP surface as update_profile: POST /api/profiles
    # hands this a decoded JSON body. `priority` is excluded because None is
    # legitimate here and means "compute the next free slot" just below.
    _validate_field_types(
        switch_threshold=switch_threshold, token_threshold=token_threshold,
        monthly_budget_cap=monthly_budget_cap, automatic=automatic,
        default_model=default_model, tag_color=tag_color, plan=plan,
        codex_model=codex_model, codex_reasoning_effort=codex_reasoning_effort,
        claude_config_dir=claude_config_dir, codex_home=codex_home,
    )
    if priority is not None:
        _validate_field_types(priority=priority)
    if not credential or len(credential.strip()) < 8:
        raise ValidationError("Credential looks too short — paste the complete token/key.")
    if priority is None:
        # Slot a new Profile in after the existing ones. Without this, a
        # caller that computes no priority would tie with whatever already
        # held the top slot. The Dashboard's Add-Profile form computes the
        # same next-free-slot value client-side.
        existing = load_pool().profiles
        priority = max((p.priority for p in existing), default=0) + 1
    if kind == "oauth" and not account_uuid:
        raise ValidationError(
            "OAuth profiles need account_uuid — it's what lets the proxy rewrite "
            "metadata.user_id so Anthropic accepts the swapped-in credential. See "
            "proxy.py's module docstring for how it's used."
        )

    profile = Profile(
        id=_new_id(),
        name=name.strip(),
        kind=kind,
        base_url=base_url,
        auth_mode=auth_mode,
        priority=priority,
        switch_threshold=switch_threshold,
        automatic=automatic,
        default_model=default_model,
        monthly_budget_cap=monthly_budget_cap,
        token_threshold=token_threshold,
        tag_color=tag_color,
        account_uuid=account_uuid,
        plan=plan,
        claude_config_dir=claude_config_dir,
        codex_home=codex_home,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
    )

    if credential_already_encoded:
        # A codex-kind (chatgpt_subscription) credential, already encoded by
        # openai_credential.encode(). That is not the Anthropic-shaped
        # oauth_credential blob below; re-encoding it would wrap a JSON
        # string inside another one and break every future decode.
        stored_credential = credential
    else:
        # refresh_token/expires_at are passed only for an OAuth credential
        # that came with them (CLI `add-account`, "Import current login"). A
        # manually pasted token leaves both None and is stored as a plain
        # string.
        stored_credential = oauth_credential.encode(oauth_credential.StoredOAuthCredential(
            access_token=credential, refresh_token=refresh_token, expires_at=expires_at))

    # Keychain first, config second. If the process dies between the two,
    # an orphaned Keychain entry with no config row is harmless, whereas a
    # config row pointing at a credential that was never written would let
    # the Router select a Profile with no secret behind it.
    try:
        secret_store.set_token(profile.id, stored_credential)
    except Exception as exc:
        raise ProfileRepositoryError(f"Could not store credential in Keychain: {exc}") from exc

    with CONFIG_LOCK:
        pool = load_pool()
        pool.profiles.append(profile)
        try:
            save_pool(pool)
        except Exception as exc:
            secret_store.delete_token(profile.id)
            raise ProfileRepositoryError(f"Could not save profile metadata, rolled back Keychain entry: {exc}") from exc

    activity.record("config", f"{profile.name} added", meta=f"kind={profile.kind}")
    return profile


def upsert_oauth_profile(*, name: str, account_uuid: str, credential: str, plan: Optional[str] = None,
                          refresh_token: Optional[str] = None, expires_at: Optional[int] = None,
                          claude_config_dir: Optional[str] = None) -> tuple[Profile, bool]:
    """The single dedup-and-upsert path every OAuth-adding flow shares (CLI
    `add-account`, "Import current login"): create a new Profile for this
    account_uuid, or, if one is already registered, refresh its credential
    and keep its plan and claude_config_dir current.

    Returns (profile, reused). reused=True means an existing Profile was
    refreshed in place, not that anything new was added."""
    existing = find_by_account_uuid(account_uuid)
    if existing is not None:
        update_credential(existing.id, credential, refresh_token=refresh_token, expires_at=expires_at)
        changes = {k: v for k, v in {"plan": plan, "claude_config_dir": claude_config_dir}.items() if v is not None}
        updated = update_profile(existing.id, **changes) if changes else existing
        return updated, True

    profile = create_profile(name=name, kind="oauth", credential=credential, account_uuid=account_uuid,
                              plan=plan, refresh_token=refresh_token, expires_at=expires_at,
                              claude_config_dir=claude_config_dir)
    return profile, False


def upsert_codex_profile(*, name: str, account_id: str, encoded_credential: str,
                          plan: Optional[str] = None, codex_home: Optional[str] = None) -> tuple[Profile, bool]:
    """The codex-kind analogue of upsert_oauth_profile(): same
    create-or-refresh-in-place shape, with the OpenAI account_id as the
    dedup key. Profile.account_uuid doubles as a generic upstream-account
    identity slot — despite the name, find_by_account_uuid() is a plain
    equality match with no kind-specific meaning.

    encoded_credential must already be openai_credential.encode()'s output;
    this function stores it as-is rather than building the blob."""
    existing = find_by_account_uuid(account_id)
    if existing is not None:
        update_credential_raw(existing.id, encoded_credential)
        changes = {k: v for k, v in {"plan": plan, "codex_home": codex_home}.items() if v is not None}
        updated = update_profile(existing.id, **changes) if changes else existing
        return updated, True

    profile = create_profile(name=name, kind="codex", credential=encoded_credential,
                              auth_mode="chatgpt_subscription", account_uuid=account_id,
                              plan=plan, codex_home=codex_home, credential_already_encoded=True)
    return profile, False


def update_profile(profile_id: str, **changes) -> Profile:
    allowed = {
        "name", "priority", "switch_threshold", "enabled", "automatic",
        "default_model", "monthly_budget_cap", "token_threshold", "tag_color", "base_url", "auth_mode", "plan",
        "claude_config_dir", "codex_home", "codex_model", "codex_reasoning_effort",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"Cannot change fields: {sorted(unknown)}")
    _validate_field_types(**changes)

    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is None:
            raise ProfileRepositoryError(f"No profile with id {profile_id!r}.")

        updated = replace(existing, **changes)
        _validate(updated.name, updated.kind, updated.base_url, updated.auth_mode, updated.tag_color)

        pool.profiles = [updated if p.id == profile_id else p for p in pool.profiles]
        save_pool(pool)

    changed = ", ".join(f"{k}={v}" for k, v in changes.items())
    activity.record("config", f"{updated.name} updated", meta=changed)
    return updated


def _renumber_priorities_sequentially(profiles: list[Profile]) -> list[Profile]:
    """Reassigns 1..N with no gaps, preserving each Profile's relative rank
    via a stable sort on the old priority. Only `.priority` changes; the
    list's own order is untouched.

    Without this, deleting a non-last Profile leaves a gap (1,2,3,5) that
    create_profile()'s max(priorities)+1 never reclaims."""
    by_old_priority = sorted(profiles, key=lambda p: p.priority)
    new_priority_by_id = {p.id: rank for rank, p in enumerate(by_old_priority, start=1)}
    return [replace(p, priority=new_priority_by_id[p.id]) for p in profiles]


def delete_profile(profile_id: str) -> None:
    with CONFIG_LOCK:
        pool = load_pool()
        existing = pool.get(profile_id)
        if existing is None:
            raise ProfileRepositoryError(f"No profile with id {profile_id!r}.")
        pool.profiles = _renumber_priorities_sequentially(
            [p for p in pool.profiles if p.id != profile_id])
        save_pool(pool)
    # Credential deleted last: if the process dies right after save_pool,
    # an orphaned Keychain entry is harmless, whereas a config row
    # referencing a missing secret is not.
    secret_store.delete_token(profile_id)
    _remove_isolated_login_artifacts(existing)
    activity.record("config", f"{existing.name} removed")


def _remove_isolated_login_artifacts(p: Profile) -> None:
    """Removes the per-account login directories a Profile owned, and the
    Keychain entry Claude Code wrote inside its isolated one.

    Both `add-account` and `add-codex-account` create a directory holding a
    LIVE refresh token — `.credentials.json` (or a directory-derived Keychain
    entry) for Claude, `auth.json` for Codex. Only `codex_home` used to be
    cleaned up here, so removing a Claude account from the Dashboard left its
    refresh token on disk and in the Keychain with nothing referencing it. Only
    `purge` cleaned those, which is not where most accounts get removed.

    Best-effort throughout: the Profile is already gone from config by this
    point, and failing to tidy up must not turn a completed delete into an
    error. Both paths are validated to live under this tool's own accounts
    roots when they are set (see _validate_isolated_dir)."""
    import shutil as _shutil

    for directory in (p.codex_home, p.claude_config_dir):
        if directory:
            _shutil.rmtree(directory, ignore_errors=True)
    if p.claude_config_dir:
        try:
            from . import anthropic_oauth
            anthropic_oauth.remove_isolated_logins([p.claude_config_dir])
        except Exception:
            pass


def reset_all_profiles() -> int:
    """The Settings page's danger-zone action. Deletes every Profile's
    Keychain credential and clears the Pool. Never touches
    shared_claude_dir or ~/.claude."""
    with CONFIG_LOCK:
        pool = load_pool()
        count = len(pool.profiles)
        removed = list(pool.profiles)
        for p in pool.profiles:
            secret_store.delete_token(p.id)
        pool.profiles = []
        save_pool(pool)
    # Same cleanup delete_profile does, for the same reason: each isolated
    # login directory holds a live refresh token, and "remove everything"
    # that leaves credentials behind has not removed everything.
    for p in removed:
        _remove_isolated_login_artifacts(p)
    activity.record("config", f"All {count} profile(s) removed (reset)")
    return count
