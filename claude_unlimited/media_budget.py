"""Fits a /v1/messages body under a Profile's request size limit by replacing
the oldest images and documents in the history with a short text note.

Screenshots are cheap in tokens and expensive in bytes (base64), so a long
session reaches the byte limit while its context window is nowhere near full,
and Claude Code's token-based auto-compact never fires. Answering 413 lets
Claude Code recover, but its recovery strips every image and compacts. Trimming
only the oldest media keeps the session going with its recent screenshots
intact; the 413 stays as the fallback for a body this cannot fit.

  - Only base64 `image`/`document` blocks are touched, including those nested
    in `tool_result` content. Text is never altered.
  - The latest message is never touched: it is what the user just sent.
  - Oldest first. The amount trimmed is rounded up to TRIM_STEP_BYTES so the
    cut-off moves every few MB of growth rather than every turn: each move
    rewrites the prompt prefix and costs a prompt-cache miss. Deterministic,
    so the same history always yields the same body.
"""

from __future__ import annotations

import json
import math

TRIM_STEP_BYTES = 4 * 1024 * 1024

# Tells the model the content is gone, so it doesn't describe the image from
# memory, and how to get it back if it still matters.
OMITTED_TEXT = ("[An earlier {kind} was removed here to keep the request under the "
                "provider's size limit and is no longer visible. Do not make claims about "
                "its contents from memory. If it is still needed, capture or read it again, "
                "or ask the user to re-attach it.]")


def fit_body(body: bytes, limit: int) -> bytes:
    """`body` unchanged when it fits, isn't a Messages JSON object, or has no
    older media to trim; otherwise a re-serialized body with the oldest media
    replaced. The result can still exceed `limit` (a large latest message, or
    a text-heavy history): the caller's size check handles that."""
    if len(body) <= limit:
        return body
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    if not isinstance(messages, list) or len(messages) < 2:
        return body

    candidates = _media(messages[:-1])
    if not candidates:
        return body

    need = math.ceil((len(body) - limit) / TRIM_STEP_BYTES) * TRIM_STEP_BYTES
    freed = 0
    for blocks, i in candidates:
        if freed >= need:
            break
        block = blocks[i]
        freed += len(block["source"]["data"])
        replacement = {"type": "text", "text": OMITTED_TEXT.format(kind=block["type"])}
        if "cache_control" in block:
            replacement["cache_control"] = block["cache_control"]
        blocks[i] = replacement

    try:
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return body


def _media(messages: list) -> list[tuple[list, int]]:
    """(containing list, index) of every base64 media block, oldest first."""
    found: list[tuple[list, int]] = []

    def walk(blocks) -> None:
        if not isinstance(blocks, list):
            return
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                walk(block.get("content"))
            elif _is_base64_media(block):
                found.append((blocks, i))

    for message in messages:
        if isinstance(message, dict):
            walk(message.get("content"))
    return found


def _is_base64_media(block: dict) -> bool:
    source = block.get("source")
    return (block.get("type") in ("image", "document") and isinstance(source, dict)
            and source.get("type") == "base64" and isinstance(source.get("data"), str))
