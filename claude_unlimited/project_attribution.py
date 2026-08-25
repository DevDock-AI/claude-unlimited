"""Per-project usage attribution: best-effort and local-only.

It rests on two facts about Claude Code:
  1. It sends its own session id in a `X-Claude-Code-Session-Id` request
     header (and, redundantly, inside the `metadata.user_id` JSON string
     proxy.py already parses for the account_uuid rewrite, under the key
     "session_id").
  2. It maintains local session transcripts at
     ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl for its resume
     feature, independent of the network request.

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

from pathlib import Path
from typing import Optional

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSION_ID_HEADER = "x-claude-code-session-id"


def session_id_from_headers(headers: dict) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == SESSION_ID_HEADER:
            return v or None
    return None


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
