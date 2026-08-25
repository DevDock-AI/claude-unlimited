from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import List, Optional

APP_DIR = Path.home() / ".claude-unlimited"
CONFIG_FILE = APP_DIR / "config.json"

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
        )
        for p in data.get("profiles", [])
    ]
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
    )

    return Pool(
        profiles=profiles,
        shared_claude_dir=data.get("shared_claude_dir", str(Path.home() / ".claude")),
        settings=settings,
    )


def save_pool(pool: Pool) -> None:
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
}


def update_settings(**changes) -> Settings:
    unknown = set(changes) - _SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"Cannot change settings fields: {sorted(unknown)}")
    if "update_mode" in changes and changes["update_mode"] not in UPDATE_MODES:
        raise ValueError(f"update_mode must be one of {UPDATE_MODES}")
    if "language" in changes:
        from . import i18n  # local import: keeps config.py's only I/O dependency (locales/) lazy

        if changes["language"] not in i18n.list_locales():
            raise ValueError(f"language must be one of {i18n.list_locales()}")

    pool = load_pool()
    pool.settings = replace(pool.settings, **changes)
    save_pool(pool)
    return pool.settings
