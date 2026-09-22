"""The 1M-context policy: which sessions may be told their route is first-party.

Pure — no I/O, no environment mutation, no clock. It has its own module because
two callers need the same answer: `cli.code` decides it when launching Claude
Code, and the daemon publishes a preview of it so the dashboard can explain why
a pool is or is not getting the full window.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional

# --- 1M context through the gateway ----------------------------------------
#
# Claude Code decides a native-1M model's window with (2.1.278, read out of the
# installed binary, not from docs):
#
#     Oy(model):  if CLAUDE_CODE_DISABLE_1M_CONTEXT      -> 200K
#                 if the model is not native-1M           -> its own window
#                 surface = Oe()                          -> "firstParty" for us
#                 if surface == "firstParty" AND fs()     -> 1M
#                 ... else a bedrock/vertex/foundry/gateway table
#     fs():       _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL set -> true
#                 else host(ANTHROPIC_BASE_URL) == "api.anthropic.com"
#
# `Oe()` does NOT look at ANTHROPIC_BASE_URL — its "gateway" value means Claude
# Code's own enterprise gateway auth, which we never set. So a `cu code` session
# is classified firstParty and fails only on fs(), because our base URL is
# 127.0.0.1. The 3p table is never reached, which is why even Sonnet 5 — the one
# model with a native_1m_3p block — is budgeted at 200K through the pool.
#
# Measured on this machine, Opus 5, same daemon, statusline `context_window`:
# 200000 without the variable, 1000000 with it. No API request is involved; the
# window is computed at startup.
#
# Setting it is a factual assertion for an oauth/api-to-Anthropic route: the
# daemon really does relay to api.anthropic.com with the user's own credential,
# so the 1M capacity Claude Code is being conservative about genuinely exists.
# It is NOT factual for a codex profile, whose backend is an OpenAI model with
# its own window and tokenizer. What makes a MIXED pool safe is the gateway's
# per-request capacity guard (docs/adr/0009, gpt_windows.py): a turn that
# would overflow a codex backend's window is never routed to it, and when
# nothing else can hold it Claude Code is answered with the prompt-too-long
# error it compacts on — hence _one_million_decision() below.
ASSUME_FIRST_PARTY_ENV = "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"
DISABLE_1M_ENV = "CLAUDE_CODE_DISABLE_1M_CONTEXT"

# The variable is underscore-prefixed: internal to Claude Code, undocumented,
# and free to disappear in any release. Gate it on versions we have actually
# read the decision out of, and degrade to "do nothing" rather than guess.
ONE_M_VERIFIED_MIN_CLIENT = (2, 1, 229)


def _parse_client_version(raw: Optional[str]) -> Optional[tuple]:
    """(major, minor, patch) from `claude --version` output, or None if it
    cannot be read. None must always mean "unverified", never "new enough"."""
    if not raw:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if match is None:
        return None
    return tuple(int(g) for g in match.groups())


def _one_million_decision(mode: str, profiles, forced_profile, env,
                          client_version: Optional[tuple]) -> tuple:
    """(should_set_the_variable, reason) for this launch.

    Pure: no I/O, no environment mutation — `env` is passed in so the whole
    policy is testable. `reason` is one of the published decision codes and is
    shown to the user, so "unknown" stays unknown.

    The route, not the plan, decides. An Anthropic account holds the window
    itself. A codex Profile in the route does not cap it any more: the
    gateway's per-request capacity guard keeps every turn that would overflow
    a GPT backend off that account (docs/adr/0009) — so a mixed pool gets 1M
    with the guard active (`enabled_1m_guarded`). A route with NOTHING that
    can hold more than a GPT window (codex only) is refused: 1M there would
    only mean every long turn ends in a prompt-too-long at ~226K instead of
    Claude Code's own compaction at 200K (`codex_only_route`).

    `force_1m` skips every route and version check: the variable is set on
    any route the user launches, the guard still applies. Only the user's
    own environment outranks it."""
    if mode == "prefer_200k":
        return False, "user_forced_200k"
    if mode == "client_default":
        return False, "client_default"

    # An explicit user choice in the environment always wins, in both
    # directions, and is never silently overridden.
    if env.get(DISABLE_1M_ENV):
        return False, "user_forced_200k"
    if env.get(ASSUME_FIRST_PARTY_ENV):
        return False, "user_already_set"

    if mode == "force_1m":
        # Whatever the route, whatever the client: the user asked. The
        # variable is harmless on a client that does not read it, and the
        # capacity guard is what keeps a codex account safe, not this check.
        return True, "forced_1m"

    if client_version is None:
        return False, "client_version_unverified"
    if client_version < ONE_M_VERIFIED_MIN_CLIENT:
        return False, "client_version_unverified"

    # Which profiles could actually serve this session? A --profile pin is a
    # guarantee of exactly one; anything else can be reached by rotation.
    if forced_profile is not None:
        route = [forced_profile]
    else:
        route = [p for p in profiles if _reachable_by_rotation(p)]
    if not route:
        return False, "no_profiles"
    codex_in_route = False
    for profile in route:
        if profile.kind == "codex":
            codex_in_route = True   # guarded per request; does not cap the window
            continue
        if profile.kind == "api" and not _is_anthropic_base_url(profile.base_url):
            # Some other Anthropic-compatible endpoint. It may well serve 1M;
            # we have no way to know, and guessing wrong is an overflow.
            return False, "custom_gateway_unknown"
    if all(p.kind == "codex" for p in route):
        return False, "codex_only_route"
    return True, "enabled_1m_guarded" if codex_in_route else "enabled_1m_verified"


def codex_profiles_in_route(profiles, forced_profile) -> list:
    """The codex Profiles the capacity guard would be sizing for a session
    launched over this route — the ones the preview and the launch note name
    with their windows. Same route rule as _one_million_decision()."""
    route = [forced_profile] if forced_profile is not None else [p for p in profiles if _reachable_by_rotation(p)]
    return [p for p in route if getattr(p, "kind", None) == "codex"]


def _reachable_by_rotation(profile) -> bool:
    """Whether routing can land on this Profile without the user naming it.

    `automatic` is the rotation flag: router.choose() and
    choose_for_new_branch() both filter candidates on it, so an enabled
    Profile without it is a manual-only account. `forced_for_subagents` is the
    exception that matters — gateway._branch_decision honours that Profile for
    every subagent whatever `automatic` says.

    This is what lets a pool holding a manual-only Codex account still get 1M:
    nothing will rotate a session onto it. It is deliberately NOT a claim that
    the session can never reach it — a mid-session "Take over", or a second
    terminal running `cu code --profile <that account>`, still can, and the
    window is already fixed by then. The line drawn here is "the pool will not
    do it on its own", which is the difference between a surprise and a
    choice."""
    if getattr(profile, "forced_for_subagents", False):
        return True
    return bool(getattr(profile, "automatic", False))


def _is_anthropic_base_url(base_url: Optional[str]) -> bool:
    """An api-kind profile with no base_url talks to Anthropic; one pointed
    elsewhere is a gateway whose capacity we cannot vouch for."""
    if not base_url or not base_url.strip():
        return True
    try:
        return urllib.parse.urlparse(base_url).hostname == "api.anthropic.com"
    except ValueError:
        return False
