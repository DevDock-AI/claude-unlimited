"""The one place a Profile "kind"'s identity is declared. See
docs/ARCHITECTURE.md for the wider connector design.

Without this registry, a kind's name lives in `_VALID_KINDS` /
`_VALID_AUTH_MODES` in profiles.py plus a scattering of `if p.kind == "..."`
branches, and adding one means grepping for every existing kind string.
This module collects a kind's *identity* into one declared place other
modules can introspect.

Scope is deliberately narrow: identity metadata only. Behavior —
transport dispatch, credential refresh, response classification — is still
owned by each kind's own modules, and the `if p.kind == "X"` branches
elsewhere remain authoritative for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


def _codex_models(parity: Optional[dict] = None) -> list[tuple[str, str]]:
    # Imported lazily so this registry stays a plain declaration that any
    # module can read without pulling in a connector's implementation.
    from . import openai_models

    return openai_models.advertised_models(parity)


@dataclass(frozen=True)
class ConnectorSpec:
    kind: str
    display_name: str  # CLI/help text only, never translated; the Dashboard's i18n owns user-facing copy
    auth_modes: tuple[str, ...]  # () if this kind has no auth_mode concept at all
    profile_fields: tuple[str, ...]  # config.Profile field names this kind actually reads/writes, beyond the shared ones every kind has
    requires_account_uuid: bool = False  # oauth only; drives proxy.py's metadata.user_id rewrite
    quota_style: str = "percent"  # "percent" (switch_threshold/DRAINING) | "token_budget" (api's token_threshold/EXHAUSTED) | "none" (kind with no usage signal at all)
    # How this kind answers GET /v1/models, which is what Claude Code builds
    # its `/model` picker from. None relays upstream unchanged (the
    # Anthropic-compatible backends: oauth, api). A callable returns
    # (model_id, display_name) pairs the daemon answers with locally, for a
    # kind whose backend speaks a different protocol.
    models_provider: Optional[Callable[[], list[tuple[str, str]]]] = None


CONNECTORS: dict[str, ConnectorSpec] = {
    "oauth": ConnectorSpec(
        kind="oauth",
        display_name="Claude subscription",
        auth_modes=(),
        profile_fields=("account_uuid", "plan", "claude_config_dir"),
        requires_account_uuid=True,
        quota_style="percent",
    ),
    "api": ConnectorSpec(
        kind="api",
        display_name="API endpoint",
        auth_modes=("api_key", "bearer"),
        profile_fields=("base_url", "auth_mode", "default_model", "monthly_budget_cap", "token_threshold"),
        quota_style="token_budget",
    ),
    "codex": ConnectorSpec(
        kind="codex",
        display_name="Codex (ChatGPT / OpenAI)",
        auth_modes=("api_key", "chatgpt_subscription"),
        profile_fields=("codex_home", "codex_model", "codex_reasoning_effort"),
        quota_style="percent",
        models_provider=_codex_models,
    ),
}


def models_listing(kind: str, parity: Optional[dict] = None) -> Optional[list[tuple[str, str]]]:
    """(model_id, display_name) pairs this kind answers GET /v1/models with,
    or None when it should be relayed to the real upstream instead.

    One dispatch point for every kind, so "what models does this connector
    offer" is answered by the registry rather than an `if p.kind ==` chain at
    the call site."""
    provider = get(kind).models_provider
    return provider(parity) if provider is not None else None


def get(kind: str) -> ConnectorSpec:
    try:
        return CONNECTORS[kind]
    except KeyError:
        raise ValueError(f"Unknown connector kind {kind!r}; must be one of {tuple(CONNECTORS)}.") from None


def all_auth_modes() -> tuple[str, ...]:
    """The union of every registered connector's auth_modes, backing
    profiles.py's _VALID_AUTH_MODES.

    Intentionally a flat union rather than a per-kind check: modes overlap
    across kinds ("api_key" belongs to both api and codex). The per-kind
    auth_mode check lives in profiles._validate()."""
    seen: list[str] = []
    for spec in CONNECTORS.values():
        for mode in spec.auth_modes:
            if mode not in seen:
                seen.append(mode)
    return tuple(seen)
