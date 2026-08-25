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
    if existing.codex_home:
        # Holds only the auth.json from the one-time `codex login`
        # handshake; the live credential lives in secret_store. Safe to
        # remove outright.
        import shutil as _shutil
        _shutil.rmtree(existing.codex_home, ignore_errors=True)
    activity.record("config", f"{existing.name} removed")


def reset_all_profiles() -> int:
    """The Settings page's danger-zone action. Deletes every Profile's
    Keychain credential and clears the Pool. Never touches
    shared_claude_dir or ~/.claude."""
    with CONFIG_LOCK:
        pool = load_pool()
        count = len(pool.profiles)
        for p in pool.profiles:
            secret_store.delete_token(p.id)
        pool.profiles = []
        save_pool(pool)
    activity.record("config", f"All {count} profile(s) removed (reset)")
    return count
