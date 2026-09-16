from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import List, Optional

APP_DIR = Path.home() / ".claude-unlimited"
CONFIG_FILE = APP_DIR / "config.json"

# The isolated per-account login directories. Defined here, once, because two
# modules need to agree on them: cli.py CREATES them during `add-account` /
# `add-codex-account`, and profiles.py REFUSES any Profile naming a directory
# outside them — delete_profile() removes codex_home recursively, so an
# unconstrained value is a request to delete a directory of someone's
# choosing. Two independent derivations of the same path would let those two
# disagree, which is exactly how the check would end up validating nothing.
_CLAUDE_ACCOUNTS_LEAF = "claude-accounts"
_CODEX_ACCOUNTS_LEAF = "codex-accounts"
CLAUDE_ACCOUNTS_DIR = APP_DIR / _CLAUDE_ACCOUNTS_LEAF
CODEX_ACCOUNTS_DIR = APP_DIR / _CODEX_ACCOUNTS_LEAF


def accounts_roots() -> tuple:
    """(claude, codex) resolved against APP_DIR **at call time**.

    The constants above are bound at import, which is what cli.py wants. The
    validator wants the live value instead, so that redirecting APP_DIR — as
    every test does — redirects what it will accept too. Otherwise the check
    would validate against the real home directory during a test run, which
    is both wrong and the sort of thing that quietly starts touching real
    files."""
    return (APP_DIR / _CLAUDE_ACCOUNTS_LEAF, APP_DIR / _CODEX_ACCOUNTS_LEAF)

# Guards the whole load_pool() -> mutate -> save_pool() cycle at the call
# sites (profiles.py), not just the I/O inside this module. The daemon runs
# one thread per connection, so without it two concurrent writes to
# /api/profiles/* can both load the same old state and drop one change, and
# their save_pool() calls can collide on the shared ".json.tmp" path and
# raise FileNotFoundError out of tmp.replace().
CONFIG_LOCK = threading.Lock()

DEFAULT_SWITCH_THRESHOLD = 98.0


@dataclass
class Profile:
    """A single routable credential entry in the Pool.

    kind is one of:
      "oauth"  a Claude Pro/Max subscription;
      "api"    any Anthropic-compatible endpoint (base_url defaults to
               Anthropic's own API and can be pointed at a gateway);
      "codex"  a ChatGPT/Codex subscription, translated to and from
               Anthropic's Messages API shape.
    """

    id: str
    name: str
    kind: str = "oauth"  # oauth | api | codex
    base_url: Optional[str] = None  # api kind only; None/"" means api.anthropic.com
    auth_mode: str = "api_key"  # api_key | bearer; api kind only
    priority: int = 1
    switch_threshold: float = DEFAULT_SWITCH_THRESHOLD
    enabled: bool = True
    automatic: bool = False  # eligible for automatic Rotation, not just manual pin
    default_model: Optional[str] = None  # api kind only, optional
    monthly_budget_cap: Optional[float] = None  # api kind only, optional
    token_threshold: Optional[int] = None  # api kind only, optional: lifetime cumulative tokens at which Rotation stops picking this Profile. The api-kind analogue of switch_threshold, since an API key has no session-percentage window to measure against.
    tag_color: Optional[str] = None  # cosmetic only
    account_uuid: Optional[str] = None  # oauth kind only: the account identity Anthropic expects in the request body
    plan: Optional[str] = None  # oauth kind only: "max" | "pro" | None (not yet detected), from anthropic_oauth.fetch_account_profile
    credential_updated_at: Optional[str] = None  # ISO timestamp bumped by profiles.update_credential(), so the live Gateway can notice a re-auth and clear a stuck AUTH_INVALID state
    claude_config_dir: Optional[str] = None  # oauth kind only: an isolated CLAUDE_CONFIG_DIR this Profile was authenticated under via `claude-unlimited add-account`, so it can be re-authenticated without touching another account's session. None for a Profile added by paste or "Import current login".
    codex_home: Optional[str] = None  # codex kind only: an isolated CODEX_HOME holding this Profile's auth.json, the counterpart of claude_config_dir. Every Codex invocation is scoped to it, so it never touches another Codex login on this machine.
    codex_model: Optional[str] = None  # codex kind only: overrides openai_models.py's mapping; None uses the automatic Claude-model -> Codex-model mapping.
    codex_reasoning_effort: Optional[str] = None  # codex kind only: overrides the reasoning-effort tier the mapping would pick (low|medium|high|xhigh|max|ultra); None uses the mapping's per-model default.
    # Route EVERY subagent (any Claude Code branch carrying an agent-id header)
    # to this Profile, whatever rotation would otherwise pick. The main agent is
    # untouched — that asymmetry is the feature: a Claude orchestrator driving
    # GPT subagents, say. At most one Profile may hold this (validated on save).
    # If it becomes unavailable, subagents fall back to the balanced branch
    # selector rather than failing. See gateway.py's branch pinning.
    forced_for_subagents: bool = False


UPDATE_MODES = ("auto_install", "auto_download", "manual")


@dataclass
class Settings:
    """Everything that isn't a Profile: update behavior and notification
    preferences. Only what a user explicitly configured; runtime state
    (daemon uptime, activity) lives elsewhere."""

    update_mode: str = "auto_download"  # auto_install | auto_download | manual
    language: str = "en"  # ISO code matching a claude_unlimited/locales/<code>.json file
    notifications_enabled: bool = True
    notify_update_available: bool = True
    notify_approaching_threshold: bool = True
    notify_rotated: bool = False
    notify_quota_reset: bool = False
    notify_needs_attention: bool = True
    # Make `code --distribute` the default for every session (UI: "Balance
    # sessions and subagents across accounts"): each new agent starts on the
    # least-busy account and stays there. OFF by default — it changes how
    # accounts are consumed, so it must be chosen, never inherited. The flag
    # stays available per-session either way; the two are OR-ed, so this
    # setting can only ever turn distribution ON, never override a `--profile`
    # pin (which outranks both).
    distribute_sessions_default: bool = False
    # Read each subscription account's usage from the providers' read-only
    # usage endpoints every 5-10 minutes while the user is active (see
    # usage_probe.py for the idle pause and backoff). On by default: it sends
    # no messages, and the Dashboard is wrong without it.
    keep_usage_fresh: bool = True
    # The editable model-parity list: an ORDERED list of rows
    # [{"claude_model", "model"?, "effort"?, "claude_effort"?}, ...] that IS the
    # set of models Claude Code's /model picker offers for Codex-served
    # sessions. Empty ([] or {}) means "the default rows" (the 4 family heads),
    # never "advertise nothing". A legacy sparse dict {claude_id: {...}} is
    # still accepted and migrated at read time by openai_models.normalize_parity
    # — the config file keeps its old shape until the user's next save writes
    # the list, so no on-load rewrite is needed.
    model_parity: object = field(default_factory=dict)


@dataclass
class Pool:
    profiles: List[Profile] = field(default_factory=list)
    shared_claude_dir: str = str(Path.home() / ".claude")
    settings: Settings = field(default_factory=Settings)

    def get(self, profile_id: str) -> Optional[Profile]:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def enabled_profiles(self) -> List[Profile]:
        return [p for p in self.profiles if p.enabled]


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError:
        pass


def normalize_forced_subagents(profiles: List[Profile]) -> List[Profile]:
    """At most one Profile may hold `forced_for_subagents` — save_pool()
    refuses more. A file can still carry several (hand-edited, or written by
    another build), and then EVERY later save would fail while routing kept
    quietly using one of them. Keep the holder routing already uses (the first
    enabled one, else the first) and clear the rest, so the next save writes
    exactly what was being routed."""
    holders = [p for p in profiles if p.forced_for_subagents]
    if len(holders) <= 1:
        return profiles
    keep = next((p for p in holders if p.enabled), holders[0])
    return [replace(p, forced_for_subagents=False) if p.forced_for_subagents and p.id != keep.id else p
            for p in profiles]


def load_pool() -> Pool:
    ensure_app_dir()
    if not CONFIG_FILE.exists():
        return Pool(profiles=[])
    data = json.loads(CONFIG_FILE.read_text())
    profiles = [
        Profile(
            id=p["id"],
            name=p["name"],
            kind=p.get("kind", "oauth"),
            base_url=p.get("base_url"),
            auth_mode=p.get("auth_mode", "api_key"),
            priority=int(p.get("priority", 1)),
            switch_threshold=float(p.get("switch_threshold", DEFAULT_SWITCH_THRESHOLD)),
            enabled=bool(p.get("enabled", True)),
            automatic=bool(p.get("automatic", False)),
            default_model=p.get("default_model"),
            monthly_budget_cap=p.get("monthly_budget_cap"),
            token_threshold=p.get("token_threshold"),
            tag_color=p.get("tag_color"),
            account_uuid=p.get("account_uuid"),
            plan=p.get("plan"),
            credential_updated_at=p.get("credential_updated_at"),
            claude_config_dir=p.get("claude_config_dir"),
            codex_home=p.get("codex_home"),
            codex_model=p.get("codex_model"),
            codex_reasoning_effort=p.get("codex_reasoning_effort"),
            forced_for_subagents=bool(p.get("forced_for_subagents", False)),
        )
        for p in data.get("profiles", [])
    ]
    profiles = normalize_forced_subagents(profiles)
    settings_data = data.get("settings", {})
    settings = Settings(
        update_mode=settings_data.get("update_mode", "auto_download"),
        language=settings_data.get("language", "en"),
        notifications_enabled=bool(settings_data.get("notifications_enabled", True)),
        notify_update_available=bool(settings_data.get("notify_update_available", True)),
        notify_approaching_threshold=bool(settings_data.get("notify_approaching_threshold", True)),
        notify_rotated=bool(settings_data.get("notify_rotated", False)),
        notify_quota_reset=bool(settings_data.get("notify_quota_reset", False)),
        notify_needs_attention=bool(settings_data.get("notify_needs_attention", True)),
        distribute_sessions_default=bool(settings_data.get("distribute_sessions_default", False)),
        keep_usage_fresh=bool(settings_data.get("keep_usage_fresh", True)),
        model_parity=settings_data.get("model_parity") or {},
    )

    return Pool(
        profiles=profiles,
        shared_claude_dir=data.get("shared_claude_dir", str(Path.home() / ".claude")),
        settings=settings,
    )


class TooManySubagentProfilesError(ValueError):
    """More than one Profile claimed `forced_for_subagents`."""


def save_pool(pool: Pool) -> None:
    # At most one Profile may be the forced subagent target: "every subagent
    # goes here" has no meaning if two Profiles claim it. Refused outright
    # rather than silently picking one, so the caller can tell the user which
    # Profile already holds it.
    forced = [p for p in pool.profiles if getattr(p, "forced_for_subagents", False)]
    if len(forced) > 1:
        raise TooManySubagentProfilesError(
            "Only one profile can be forced for subagents; already set on: "
            + ", ".join(p.name for p in forced))
    ensure_app_dir()
    payload = {
        "profiles": [asdict(p) for p in pool.profiles],
        "shared_claude_dir": pool.shared_claude_dir,
        "settings": asdict(pool.settings),
    }
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CONFIG_FILE)


_SETTINGS_FIELDS = {
    "update_mode", "language", "notifications_enabled", "notify_update_available",
    "notify_approaching_threshold", "notify_rotated", "notify_quota_reset", "notify_needs_attention",
    "distribute_sessions_default", "keep_usage_fresh", "model_parity",
}


def _validated_model_row_fields(where, row, entry):
    """Validate the model/effort/claude_effort of one parity row into `entry`.
    Shared by the dict (legacy) and list (current) shapes."""
    from .openai_models import VALID_REASONING_EFFORTS, CLAUDE_REASONING_EFFORTS

    model = row.get("model")
    if model is not None:
        # Left free-form on purpose: a Profile override already accepts an
        # arbitrary model id, and pinning one this build has not heard of is
        # legitimate. The fallback ladder handles a rejected model.
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError(f"{where}.model must be a short non-empty string")
        entry["model"] = model.strip()
    effort = row.get("effort")
    if effort is not None:
        if effort not in VALID_REASONING_EFFORTS:
            raise ValueError(f"{where}.effort must be one of {list(VALID_REASONING_EFFORTS)}")
        entry["effort"] = effort
    claude_effort = row.get("claude_effort")
    if claude_effort is not None:
        if claude_effort not in CLAUDE_REASONING_EFFORTS:
            raise ValueError(
                f"{where}.claude_effort must be one of {list(CLAUDE_REASONING_EFFORTS)}")
        entry["claude_effort"] = claude_effort
    return entry


def _validated_model_parity(raw):
    """Validates a parity payload, rejecting the whole thing rather than
    silently dropping a bad row — a mapping that half-applied would be worse
    than one that refused.

    Accepts BOTH shapes so old config files and export bundles keep working:
      * the current ORDERED LIST of rows [{claude_model, model?, effort?,
        claude_effort?}, ...] — the editable parity list; order is preserved;
      * the legacy sparse dict {claude_id: {model?, effort?, claude_effort?}}.
    openai_models.normalize_parity() turns either into the in-force row list at
    read time, and the empty case ([]/{}) means "the default rows"."""
    from .model_catalogue import base_id

    if isinstance(raw, list):
        if len(raw) > 64:
            raise ValueError("model_parity has too many entries")
        cleaned_list: list = []
        seen_bases: set = set()
        for i, row in enumerate(raw):
            if not isinstance(row, dict):
                raise ValueError(f"model_parity[{i}] must be an object")
            claude_id = row.get("claude_model")
            if not isinstance(claude_id, str) or not claude_id.strip() or len(claude_id) > 128:
                raise ValueError(f"model_parity[{i}].claude_model must be a short non-empty model id")
            claude_id = claude_id.strip()
            base = base_id(claude_id)
            if base in seen_bases:
                raise ValueError(f"model_parity has a duplicate Claude model: {claude_id}")
            seen_bases.add(base)
            entry = {"claude_model": claude_id}
            _validated_model_row_fields(f"model_parity[{i}]", row, entry)
            cleaned_list.append(entry)
        return cleaned_list

    if not isinstance(raw, dict):
        raise ValueError("model_parity must be a list of rows or an object")
    if len(raw) > 64:
        raise ValueError("model_parity has too many entries")
    cleaned: dict = {}
    for claude_id, row in raw.items():
        if not isinstance(claude_id, str) or not claude_id.strip():
            raise ValueError("model_parity keys must be non-empty model ids")
        if not isinstance(row, dict):
            raise ValueError(f"model_parity[{claude_id}] must be an object")
        entry = _validated_model_row_fields(f"model_parity[{claude_id}]", row, {})
        if entry:
            cleaned[claude_id.strip()] = entry
    return cleaned


def validated_settings_changes(changes: dict) -> dict:
    """Validates a settings payload. Shared by PATCH /api/settings and by
    bundle import, so an imported bundle cannot set something the API would
    have refused."""
    unknown = set(changes) - _SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"Cannot change settings fields: {sorted(unknown)}")
    changes = dict(changes)
    if "keep_usage_fresh" in changes and not isinstance(changes["keep_usage_fresh"], bool):
        raise ValueError("keep_usage_fresh must be true or false")
    if "update_mode" in changes and changes["update_mode"] not in UPDATE_MODES:
        raise ValueError(f"update_mode must be one of {UPDATE_MODES}")
    if "model_parity" in changes:
        changes["model_parity"] = _validated_model_parity(changes["model_parity"])
    if "language" in changes:
        from . import i18n

        if changes["language"] not in i18n.list_locales():
            raise ValueError(f"language must be one of {i18n.list_locales()}")
    return changes


def update_settings(**changes) -> Settings:
    changes = validated_settings_changes(changes)
    with CONFIG_LOCK:
        pool = load_pool()
        pool.settings = replace(pool.settings, **changes)
        save_pool(pool)
        return pool.settings
