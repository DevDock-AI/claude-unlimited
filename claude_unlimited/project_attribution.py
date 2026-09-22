"""Per-project usage attribution: best-effort and local-only.

It rests on three facts about Claude Code:
  1. It sends its own session id in a `X-Claude-Code-Session-Id` request
     header (and, redundantly, inside the `metadata.user_id` JSON string
     proxy.py already parses for the account_uuid rewrite, under the key
     "session_id").
  2. It maintains local session transcripts at
     ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl for its resume
     feature, independent of the network request.
  3. A SUBAGENT's requests carry an `x-claude-code-agent-id` header (and
     `x-claude-code-parent-agent-id` when nested); the MAIN agent sends
     neither, so header absence is itself the "this is the main branch"
     signal. Verified live against Claude Code 2.1.267 through this daemon:
     the id is stable for the whole life of one subagent (every turn it
     takes), and the session id above is lineage-stable — main and all its
     subagents share it. That pair is what identifies a conversation
     *branch* for per-branch account pinning (see gateway.py's branch pins).

Correlating the two attributes a request to a project directory using only a
header value and a local filename; no request body or conversation content is
read. There is no structured `cwd` field in the request to use instead — the
working directory appears only incidentally inside free-form system prompt
text, which is not something to depend on.

Best-effort by construction: a session with no flushed transcript yet, a
non-Claude-Code client, or a Claude Code version that renames the header all
resolve to no attribution rather than an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSION_ID_HEADER = "x-claude-code-session-id"
# Any other client can opt into the same per-branch routing by naming its own
# stable id — one per worker, queue or conversation. Without it a non-Claude-
# Code app shares the pool's single rotation pointer with everything else, so a
# burst of concurrent requests lands on whichever account the pointer happened
# to hold, and the provider's prompt cache (which is per account) never warms.
APP_SESSION_HEADER = "x-claude-unlimited-session"
AGENT_ID_HEADER = "x-claude-code-agent-id"
PARENT_AGENT_ID_HEADER = "x-claude-code-parent-agent-id"

# The branch id used when no agent-id header is present: Claude Code's MAIN
# agent sends none, so absence is the signal rather than a missing value.
MAIN_BRANCH = "main"


def _header(headers: dict, name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name:
            return v or None
    return None


def session_id_from_headers(headers: dict) -> Optional[str]:
    return _header(headers, SESSION_ID_HEADER)


def app_session_id(headers: dict) -> Optional[str]:
    """The caller's own branch id (APP_SESSION_HEADER), for a client that is
    not Claude Code."""
    return _header(headers, APP_SESSION_HEADER)


def agent_id_from_headers(headers: dict) -> str:
    """The requesting branch's agent id, or MAIN_BRANCH when absent.

    Absence is meaningful, not missing data: Claude Code attaches this header
    only for subagents (fact 3 above), so no header == the main agent."""
    return _header(headers, AGENT_ID_HEADER) or MAIN_BRANCH


def parent_agent_id_from_headers(headers: dict) -> Optional[str]:
    """Only present for nested subagents. Display/telemetry only — a branch is
    keyed by its OWN agent id, never its parent's."""
    return _header(headers, PARENT_AGENT_ID_HEADER)


def is_subagent(headers: dict) -> bool:
    return _header(headers, AGENT_ID_HEADER) is not None


def lineage_session_id(headers: dict, body: bytes = b"", parsed: Optional[dict] = None) -> Optional[str]:
    """The session id shared by a whole conversation tree (main + subagents).

    Prefers the body's `metadata.user_id.session_id`, which Claude Code keeps
    lineage-stable across subagents AND across a resumed session, and falls
    back to the header. Best-effort by construction, like everything else
    here: any parse failure returns None, and the caller then routes the
    request exactly as it would have before (never an error)."""
    from_app = app_session_id(headers)
    if from_app:
        return from_app
    if parsed is not None or body:
        try:
            # `parsed` is the caller's single parse of this body, passed in so
            # model-aware routing does not cost a second one. Without it this parses, exactly as before.
            root = parsed if parsed is not None else json.loads(body)
            user_id = root.get("metadata", {}).get("user_id")
            if isinstance(user_id, str):
                session_id = json.loads(user_id).get("session_id")
                if isinstance(session_id, str) and session_id:
                    return session_id
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
            pass
    return session_id_from_headers(headers)


def branch_key(headers: dict, body: bytes = b"", parsed: Optional[dict] = None) -> Optional[tuple]:
    """`(lineage_session_id, agent_id)` — the identity of one conversation
    branch, or None when this isn't identifiable Claude Code traffic (in which
    case the request must route unpinned, through the normal path)."""
    session_id = lineage_session_id(headers, body, parsed)
    if not session_id:
        return None
    return (session_id, agent_id_from_headers(headers))


def resolve_project(session_id: str) -> Optional[str]:
    """Returns the sanitized project-directory name (Claude Code's own
    encoding, e.g. "-Users-alice-code-my-app") whose local session
    transcripts include this session id, or None if it can't be resolved."""
    if not session_id or not PROJECTS_DIR.is_dir():
        return None
    for entry in PROJECTS_DIR.iterdir():
        if entry.is_dir() and (entry / f"{session_id}.jsonl").exists():
            return entry.name
    return None


def display_name(sanitized: str) -> str:
    """Best-effort human-friendly label for a sanitized project directory
    name. Claude Code's sanitization replaces "/" with "-", which is lossy
    for any path component that itself contains a hyphen, so a naive
    reversal would split "my-app" into "my/app".

    Reconstructs the path by walking left to right and consulting the
    filesystem at each "-": if extending the current directory by the next
    segment exists on disk, that "-" was a "/"; otherwise it was a literal
    hyphen and the segment keeps growing. Only a split the filesystem
    confirms is trusted. Falls back to the raw sanitized form if the walk
    never lands on a real directory (renamed, moved or deleted since Claude
    Code wrote it) rather than assert a path that may not exist."""
    parts = sanitized.lstrip("-").split("-")
    if not parts or parts == [""]:
        return sanitized.lstrip("-") or sanitized

    current = Path("/")
    segment = parts[0]
    for part in parts[1:]:
        candidate = current / segment
        if candidate.is_dir():
            current = candidate
            segment = part
        else:
            segment = f"{segment}-{part}"

    final = current / segment
    if final.is_dir():
        return final.name
    return sanitized.lstrip("-")
