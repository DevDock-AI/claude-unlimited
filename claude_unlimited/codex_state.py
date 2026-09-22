"""The conversation state the Codex CLI keeps and a stateless bridge would lose.

Claude Code re-sends the whole conversation every request and holds no
OpenAI-specific state. The real Codex CLI does hold some, and three pieces of
it decide how much of a Codex subscription a conversation burns:

* **Stable conversation identity.** Codex sends the same `session-id` for a
  whole session and the same `thread-id` for a thread; ChatGPT derives cache
  affinity from the session id and `prompt_cache_key`. A fresh random id per
  request (what this bridge used to send) gives the backend no reason to route
  a conversation's requests to where its prefix is cached.
* **Encrypted reasoning, passed back.** With `store: false`, Codex asks for
  `reasoning.encrypted_content` and replays those reasoning items before the
  output they preceded. Without them, every step of a tool loop reasons again
  from scratch — and reasoning output is what the Codex quota charges most for.
* **The sticky-routing turn token.** The backend returns `x-codex-turn-state`
  on a turn's first response; Codex replays it for every request of that turn
  and never into the next one.

This module is that memory, keyed by Claude Code's own conversation identity
(session id + agent id, from project_attribution). In memory only and bounded:
a daemon restart costs one uncached request per conversation, never an error.
Reasoning is only ever replayed to the SAME account and model that produced
it — encrypted reasoning is not portable across either.

Reference: openai/codex `codex-rs/core/src/client.rs` (build_responses_request,
prompt_cache_key, ModelClientSession.turn_state) and
`codex-rs/codex-api/src/requests/headers.rs`.
"""

from __future__ import annotations

import hashlib
import os
import uuid
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

MAIN_AGENT = "main"

# Bounds. A branch is one conversation (main agent or one subagent); a
# reasoning entry is one model turn's reasoning items.
MAX_BRANCHES = 512
MAX_REASONING_ENTRIES = 4096
MAX_REASONING_BYTES = 64 * 1024 * 1024
REASONING_TTL_SECONDS = 12 * 3600

_lock = threading.Lock()


def _uuid7() -> str:
    """UUIDv7, the shape the Codex CLI uses for session and thread ids."""
    unix_ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = unix_ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9:16] = rand[3:10]
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


@dataclass(frozen=True)
class ConversationIds:
    session_id: str               # shared by a Claude Code session and all its subagents
    thread_id: str                # this branch
    parent_thread_id: Optional[str]  # the main agent's thread, for a subagent
    prompt_cache_key: str
    is_subagent: bool


_sessions: "OrderedDict[str, str]" = OrderedDict()           # claude session -> codex session id
_threads: "OrderedDict[tuple, str]" = OrderedDict()          # (claude session, agent) -> thread id
_turn_state: "OrderedDict[tuple, str]" = OrderedDict()       # (claude session, agent, turn) -> token
_reasoning: "OrderedDict[tuple, tuple[float, list, int]]" = OrderedDict()
_reasoning_bytes = 0


def _remember(store: OrderedDict, key, make, cap: int):
    if key in store:
        store.move_to_end(key)
        return store[key]
    value = make()
    store[key] = value
    while len(store) > cap:
        store.popitem(last=False)
    return value


def conversation_ids(claude_session_id: Optional[str], agent_id: Optional[str],
                     parent_agent_id: Optional[str] = None) -> Optional[ConversationIds]:
    """Stable Codex identity for one Claude Code branch, or None when the
    request is not identifiable Claude Code traffic (the caller then sends
    per-request ids, as before).

    A subagent's parent thread is the agent that spawned it: the main agent
    for a direct subagent (Claude Code sends no parent id then), its real
    parent for a nested one — which used to be reported as the main thread."""
    if not claude_session_id:
        return None
    agent = agent_id or MAIN_AGENT
    parent = parent_agent_id or MAIN_AGENT
    with _lock:
        session = _remember(_sessions, claude_session_id, _uuid7, MAX_BRANCHES)
        main_thread = _remember(_threads, (claude_session_id, MAIN_AGENT), _uuid7, MAX_BRANCHES)
        thread = main_thread if agent == MAIN_AGENT else _remember(
            _threads, (claude_session_id, agent), _uuid7, MAX_BRANCHES)
        parent_thread = main_thread if parent == MAIN_AGENT else _remember(
            _threads, (claude_session_id, parent), _uuid7, MAX_BRANCHES)
    is_subagent = agent != MAIN_AGENT
    return ConversationIds(
        session_id=session,
        thread_id=thread,
        parent_thread_id=parent_thread if is_subagent else None,
        # Codex's root key is the session id; each branch gets its own here,
        # because a subagent's prefix (its own system prompt and task) shares
        # nothing with the main agent's.
        prompt_cache_key=session if not is_subagent else f"{session}:{thread}",
        is_subagent=is_subagent,
    )


# Below this, a system prompt is too small to be worth keying on (OpenAI's
# prefix cache needs ~1024 tokens of shared prefix before it engages at all).
MIN_PROMPT_KEY_CHARS = 2000
_PROMPT_NAMESPACE = uuid.UUID("6f6e8b9e-1a4f-5c3d-9f2b-1d0c7a4e55aa")


def identity_from_prompt(model: str, instructions: str) -> Optional[ConversationIds]:
    """Cache identity for traffic that is NOT Claude Code.

    A script calling the proxy directly carries no session or agent headers,
    so there is no conversation to key on — and a fresh random session-id per
    request is exactly what stops ChatGPT keeping those requests near their
    cached prefix. Requests that share a big system prompt are the same prefix
    from the backend's point of view, so the prompt itself becomes the key:
    identical instructions on the same model get identical ids, every time and
    in every process. None when there is not enough prompt to be worth it.
    """
    if not instructions or len(instructions) < MIN_PROMPT_KEY_CHARS:
        return None
    digest = hashlib.sha256(f"{model}\n{instructions}".encode("utf-8")).hexdigest()
    session = str(uuid.uuid5(_PROMPT_NAMESPACE, digest))
    return ConversationIds(
        session_id=session,
        thread_id=str(uuid.uuid5(_PROMPT_NAMESPACE, "thread:" + digest)),
        parent_thread_id=None,
        prompt_cache_key=f"cu-prompt-{digest[:16]}",
        is_subagent=False,
    )


# ---- sticky routing within a turn -------------------------------------------

def turn_state(claude_session_id: str, agent_id: Optional[str], turn: int) -> Optional[str]:
    with _lock:
        return _turn_state.get((claude_session_id, agent_id or MAIN_AGENT, turn))


def remember_turn_state(claude_session_id: str, agent_id: Optional[str], turn: int, token: str) -> None:
    """Only the FIRST token of a turn is kept: Codex stores it in a OnceLock
    and replays that value unchanged for the rest of the turn."""
    if not token:
        return
    with _lock:
        key = (claude_session_id, agent_id or MAIN_AGENT, turn)
        if key not in _turn_state:
            _remember(_turn_state, key, lambda: token, MAX_BRANCHES * 4)


# ---- encrypted reasoning ----------------------------------------------------

def anchor_for_text(text: str) -> str:
    return "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def anchor_for_call(call_id: str) -> str:
    return "call:" + call_id


def remember_reasoning(profile_id: str, model: str, anchors: list[str], items: list[dict]) -> None:
    """Keep one model turn's reasoning items, reachable from each anchor of
    the output they preceded (its tool call ids, or its text)."""
    global _reasoning_bytes
    items = [i for i in items if isinstance(i, dict) and i.get("encrypted_content")]
    if not items or not anchors:
        return
    size = sum(len(i.get("encrypted_content") or "") for i in items)
    now = time.monotonic()
    with _lock:
        for anchor in anchors:
            key = (profile_id, model, anchor)
            old = _reasoning.pop(key, None)
            if old:
                _reasoning_bytes -= old[2]
            _reasoning[key] = (now, items, size)
            _reasoning_bytes += size
        while _reasoning and (len(_reasoning) > MAX_REASONING_ENTRIES or _reasoning_bytes > MAX_REASONING_BYTES):
            _, (_, _, dropped) = _reasoning.popitem(last=False)
            _reasoning_bytes -= dropped


def reasoning_for(profile_id: str, model: str, anchors: list[str]) -> list[dict]:
    """The reasoning items that preceded an assistant message, found through
    any of its anchors; empty when unknown, expired, or from another account
    or model."""
    now = time.monotonic()
    with _lock:
        for anchor in anchors:
            entry = _reasoning.get((profile_id, model, anchor))
            if entry is None:
                continue
            stamp, items, _ = entry
            if now - stamp > REASONING_TTL_SECONDS:
                continue
            return list(items)
    return []


def clear() -> None:
    """Tests only."""
    global _reasoning_bytes
    with _lock:
        _sessions.clear()
        _threads.clear()
        _turn_state.clear()
        _reasoning.clear()
        _reasoning_bytes = 0
