"""Pure translation between Anthropic's Messages API shape (what Claude Code
sends and expects) and OpenAI's Responses API shape (what a codex-kind
Profile talks to). No I/O, no network, no subprocess — the OpenAI-side
counterpart of proxy.py's pure request-building and usage_tracking.py's pure
event parsing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Optional, Callable

from .openai_models import OpenAIModelTarget


# ---- Anthropic request -> OpenAI Responses API request ----

def anthropic_request_to_openai(body: dict, target: OpenAIModelTarget, *,
                                reasoning_lookup: Optional[Callable[[list[str]], list[dict]]] = None,
                                prompt_cache_key: Optional[str] = None) -> dict:
    """Translates one already-JSON-decoded Anthropic /v1/messages request
    body into an OpenAI Responses API request body.

    Every Claude Code request carries the full conversation, since the
    Anthropic API requires history to be re-sent each turn. It is translated
    faithfully into OpenAI's `input` item list, never summarized or
    truncated.

    `reasoning_lookup(anchors)` returns the encrypted reasoning items that
    preceded an earlier assistant message (codex_state); they are replayed
    right before that message's items, the order the Codex CLI sends them in.
    `prompt_cache_key` is the conversation's stable cache key."""
    instructions = _extract_system_text(body.get("system"))
    input_items: list[dict] = []
    for message in body.get("messages", []):
        if reasoning_lookup is not None and isinstance(message, dict) and message.get("role") == "assistant":
            input_items.extend(reasoning_lookup(assistant_anchors(message)))
        input_items.extend(_message_to_input_items(message))

    openai_body: dict = {
        "model": target.model,
        "input": input_items,
        "stream": True,
        "store": False,
        "tool_choice": _map_tool_choice(body.get("tool_choice")),
        "parallel_tool_calls": True,
        "reasoning": {"effort": target.reasoning_effort},
        # With store:false the backend keeps nothing between requests; this
        # returns each turn's reasoning encrypted, so it can be replayed.
        "include": ["reasoning.encrypted_content"],
    }
    if prompt_cache_key:
        openai_body["prompt_cache_key"] = prompt_cache_key
    if instructions:
        openai_body["instructions"] = instructions
    tools = _map_tools(body.get("tools"))
    if tools:
        openai_body["tools"] = tools
    return openai_body


def assistant_anchors(message: dict) -> list[str]:
    """How an assistant message is recognised when Claude Code sends it back:
    by its tool call ids (unique, and ours — the translator emits OpenAI's
    call_id as the tool_use id), else by its exact text."""
    from .codex_state import anchor_for_call, anchor_for_text
    content = message.get("content")
    if isinstance(content, str):
        return [anchor_for_text(content)] if content else []
    if not isinstance(content, list):
        return []
    anchors = [anchor_for_call(b["id"]) for b in content
               if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if text:
        anchors.append(anchor_for_text(text))
    return anchors


def user_turn_index(body: dict) -> int:
    """How many messages the user has typed so far — a new one starts a new
    Codex turn. Tool results sent back do not."""
    count = 0
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            count += 1
        elif isinstance(content, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            count += 1
    return count


def _extract_system_text(system) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [block.get("text", "") for block in system if isinstance(block, dict) and block.get("type") == "text"]
        return "\n\n".join(p for p in parts if p)
    return ""


def _message_to_input_items(message: dict) -> list[dict]:
    role = message.get("role", "user")
    content = message.get("content")
    if isinstance(content, str):
        return [_text_message_item(role, content)]
    if not isinstance(content, list):
        return []

    items: list[dict] = []
    text_parts: list[dict] = []

    def _flush_text():
        if text_parts:
            items.append({"type": "message", "role": _openai_role(role), "content": text_parts.copy()})
            text_parts.clear()

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(_text_part(role, block.get("text", "")))
        elif block_type == "image":
            text_parts.append(_image_part(block))
        elif block_type == "tool_use":
            # A prior assistant turn's tool call must be its own input item
            # (OpenAI's function_call shape), not folded into a message's
            # content array.
            _flush_text()
            items.append({
                "type": "function_call",
                "call_id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {})),
            })
        elif block_type == "tool_result":
            # Anthropic's tool_result maps to OpenAI's function_call_output,
            # keyed by the same call id (Anthropic names it tool_use_id).
            _flush_text()
            items.append({
                "type": "function_call_output",
                "call_id": block.get("tool_use_id", ""),
                "output": _tool_result_text(block.get("content")),
            })
        # Any other block type is skipped rather than raised on: losing one
        # unrecognized content block beats failing the whole request.
    _flush_text()
    return items


def _openai_role(anthropic_role: str) -> str:
    return "assistant" if anthropic_role == "assistant" else "user"


def _text_message_item(role: str, text: str) -> dict:
    return {"type": "message", "role": _openai_role(role), "content": [_text_part(role, text)]}


def _text_part(role: str, text: str) -> dict:
    # OpenAI distinguishes input_text (sent in) from output_text (produced
    # by the assistant) within the same `content` array shape: an assistant
    # message replayed as history uses output_text, everything else
    # input_text.
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": kind, "text": text}


def _image_part(block: dict) -> dict:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")
        return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"}
    if source.get("type") == "url":
        return {"type": "input_image", "image_url": source.get("url", "")}
    return {"type": "input_text", "text": "[image omitted — unrecognized source shape]"}


def _tool_result_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return str(content)


# Tools Anthropic runs on its own servers rather than handing back to the
# client. They arrive with no input_schema and a *versioned* type
# (`web_search_20250305` today), and OpenAI's Responses API has its own
# built-in equivalents that are declared by type alone. Translating one of
# these into an ordinary function tool produced a tool the model was told to
# call and that nothing could execute.
# Keyed by the Anthropic type's family, because Anthropic versions these with
# a date and bumps it on revisions: web search arrives as `web_search_20250305`
# on older models and `web_search_20260209` on newer ones, and both mean the
# same thing here. A value of None means Anthropic has a server tool that
# OpenAI has no equivalent for.
SERVER_TOOL_EQUIVALENTS = {
    "web_search": "web_search",
    # OpenAI's Responses API has no fetch-this-URL built-in. Sending it on as a
    # function tool would offer the model something nothing can execute, so the
    # tool is dropped instead: the model simply cannot fetch on a codex Profile.
    "web_fetch": None,
}


def _server_tool_type(tool: dict):
    """(is_server_tool, openai_type) for an Anthropic tool definition.

    `openai_type` is None either because this is an ordinary client tool
    (is_server_tool False) or because OpenAI has no equivalent (True)."""
    kind = tool.get("type") or ""
    for family, openai_type in SERVER_TOOL_EQUIVALENTS.items():
        if kind.startswith(family):
            return True, openai_type
    return False, None


def _map_tool_choice(anthropic_tool_choice):
    """Anthropic tool_choice -> Responses API tool_choice.

    Returns a plain string for the three blanket modes, or an object naming
    one tool. Returning the bare tool name — which this did until it was
    caught on a forced `web_search` — is not a shape the API accepts at all:
    it answers `Invalid value: 'web_search'. Supported values are: 'none',
    'auto', and 'required'.` and fails the whole request, for any forced tool."""
    if not isinstance(anthropic_tool_choice, dict):
        return "auto"
    kind = anthropic_tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    name = anthropic_tool_choice.get("name")
    if kind == "tool" and name:
        hosted = SERVER_TOOL_EQUIVALENTS.get(name)
        if hosted:
            # A hosted tool is chosen by its type; it is not a function.
            return {"type": hosted}
        if name in SERVER_TOOL_EQUIVALENTS:
            # Forcing a server tool we dropped would name a tool that is not in
            # the request at all. Let the model choose from what it does have.
            return "auto"
        return {"type": "function", "name": name}
    return "auto"


def _builtin_tool(tool: dict, openai_type: str) -> dict:
    """An Anthropic server-side tool as OpenAI's built-in equivalent, carrying
    over the options both sides express."""
    spec: dict = {"type": openai_type}
    allowed = tool.get("allowed_domains")
    if isinstance(allowed, list) and allowed:
        spec["filters"] = {"allowed_domains": allowed}
    # `blocked_domains` is deliberately not translated: OpenAI has no
    # deny-list equivalent, and silently inverting it into an allow-list would
    # widen a restriction the caller asked for. It is dropped, not guessed at.
    location = tool.get("user_location")
    if isinstance(location, dict):
        spec["user_location"] = location
    return spec


def _map_tools(anthropic_tools) -> list[dict]:
    if not isinstance(anthropic_tools, list):
        return []
    tools = []
    for t in anthropic_tools:
        if not isinstance(t, dict):
            continue
        is_server_tool, builtin = _server_tool_type(t)
        if is_server_tool:
            if builtin:
                tools.append(_builtin_tool(t, builtin))
            continue
        if not t.get("name"):
            continue
        tools.append({
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            "strict": False,
        })
    return tools


# ---- OpenAI SSE response -> Anthropic SSE response ----

@dataclass
class TranslatedUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # Reasoning is part of output_tokens; carried separately because it is
    # what drives Codex quota (ADR 0007).
    reasoning_tokens: int = 0


@dataclass
class ResponseTranslator:
    """Stateful per-request translator from OpenAI Responses API SSE events
    to Anthropic Messages API SSE events.

    feed() takes one parsed OpenAI event and yields zero or more
    `event: ... / data: ...`-framed byte chunks, ready to write straight to
    the client. Fully incremental: OpenAI's token-level
    response.output_text.delta events are forwarded as Anthropic
    content_block_delta events as they arrive, never buffered to
    completion."""

    message_id: str = "msg_openai_bridge"
    model: str = ""
    _started: bool = False
    _current_block_index: int = -1
    _current_block_open: bool = False
    _current_block_type: Optional[str] = None  # "text" | "tool_use"
    _stop_reason: str = "end_turn"
    usage: TranslatedUsage = field(default_factory=TranslatedUsage)
    _saw_any_output: bool = False
    # A message item has been opened upstream but has produced no text yet, so
    # no Anthropic block has been started for it. See _start_block.
    _pending_text_block: bool = False
    # What this response produced that a later request must be able to match:
    # the reasoning items to replay, and the anchors they belong to.
    reasoning_items: list = field(default_factory=list)
    call_ids: list = field(default_factory=list)
    text: str = ""

    def anchors(self) -> list[str]:
        from .codex_state import anchor_for_call, anchor_for_text
        return [anchor_for_call(c) for c in self.call_ids] + ([anchor_for_text(self.text)] if self.text else [])

    def feed(self, event: dict) -> Iterator[bytes]:
        event_type = event.get("type", "")
        if event_type == "response.created":
            yield from self._start_message(event)
        elif event_type == "response.output_item.added":
            yield from self._start_block(event)
        elif event_type == "response.output_text.delta":
            yield from self._text_delta(event)
        elif event_type == "response.function_call_arguments.delta":
            yield from self._tool_input_delta(event)
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "reasoning" and item.get("encrypted_content"):
                # Kept exactly as returned (id, summary, encrypted_content),
                # the shape the Codex CLI replays.
                self.reasoning_items.append({k: item[k] for k in ("type", "id", "summary", "encrypted_content")
                                             if k in item})
            yield from self._end_block(event)
        elif event_type == "response.completed":
            yield from self._finish(event, stop_reason=None)
        elif event_type == "response.failed" or event_type == "response.incomplete":
            yield from self._finish(event, stop_reason="error")
        # response.in_progress, response.content_part.*,
        # response.output_text.done and any other event carry nothing this
        # translator needs, so they are skipped. OpenAI's event set keeps
        # growing and an unrecognized type must never break the stream.

    def _sse(self, event_name: str, data: dict) -> bytes:
        return f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")

    def abort(self, message: str) -> Iterator[bytes]:
        """End a stream that broke after it started, as an error the client
        can see.

        The alternative is what used to happen: the connection to the provider
        fails mid-response (a stalled read, a reset), the generator raises, and
        the client is left holding a stream whose `message_stop` never
        arrives — a subagent that sits on "Waiting for task" forever. An
        `error` event is a terminal event in Anthropic's SSE protocol, so the
        turn fails in seconds instead of hanging.
        """
        if self._current_block_open:
            yield self._sse("content_block_stop",
                            {"type": "content_block_stop", "index": self._current_block_index})
            self._current_block_open = False
        yield self._sse("error", {"type": "error",
                                  "error": {"type": "api_error", "message": message}})

    def _start_message(self, event: dict) -> Iterator[bytes]:
        if self._started:
            return
        self._started = True
        response = event.get("response", {})
        self.model = response.get("model", self.model)
        yield self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id, "type": "message", "role": "assistant",
                "content": [], "model": self.model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _start_block(self, event: dict) -> Iterator[bytes]:
        item = event.get("item", {})
        item_type = item.get("type")
        if item_type == "message":
            # Opened only once text actually arrives (see _text_delta). A
            # message item that produces none — the model went straight to a
            # tool call, or the turn ended early — would otherwise become an
            # empty text block, which Claude Code keeps in its history and
            # sends back on the NEXT request: Anthropic rejects that with
            # "400 messages: text content blocks must be non-empty", so a
            # single empty block from here breaks the conversation on a
            # different account entirely.
            self._current_block_type = "text"
            self._pending_text_block = True
        elif item_type == "function_call":
            self._current_block_type = "tool_use"
            self._current_block_index += 1
            self._current_block_open = True
            # Never an empty id: the client matches the tool_result it sends
            # back against this value, and an empty one silently breaks that
            # pairing. Claude Code 2.1.246 also fixed a render error on a
            # tool_use block arriving without an id from a third-party
            # ANTHROPIC_BASE_URL, which is exactly what this proxy is.
            self._pending_tool_call_id = (
                item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex}"
            )
            self._pending_tool_name = item.get("name", "")
            self.call_ids.append(self._pending_tool_call_id)
            yield self._sse("content_block_start", {
                "type": "content_block_start", "index": self._current_block_index,
                "content_block": {"type": "tool_use", "id": self._pending_tool_call_id,
                                   "name": self._pending_tool_name, "input": {}},
            })
        # web_search, reasoning and other item types open no Anthropic
        # block.

    def _text_delta(self, event: dict) -> Iterator[bytes]:
        delta = event.get("delta", "")
        if not delta:
            return
        if self._pending_text_block:
            self._pending_text_block = False
            self._current_block_index += 1
            self._current_block_open = True
            yield self._sse("content_block_start", {
                "type": "content_block_start", "index": self._current_block_index,
                "content_block": {"type": "text", "text": ""},
            })
        if self._current_block_index < 0:
            return
        self._saw_any_output = True
        self.text += delta
        yield self._sse("content_block_delta", {
            "type": "content_block_delta", "index": self._current_block_index,
            "delta": {"type": "text_delta", "text": delta},
        })

    def _tool_input_delta(self, event: dict) -> Iterator[bytes]:
        delta = event.get("delta", "")
        if not delta or self._current_block_index < 0:
            return
        self._saw_any_output = True
        self._stop_reason = "tool_use"
        yield self._sse("content_block_delta", {
            "type": "content_block_delta", "index": self._current_block_index,
            "delta": {"type": "input_json_delta", "partial_json": delta},
        })

    def _end_block(self, event: dict) -> Iterator[bytes]:
        # A message item that never produced text: nothing was started, so
        # nothing is stopped, and no empty block reaches the client.
        self._pending_text_block = False
        if not self._current_block_open:
            return
        item = event.get("item", {})
        if item.get("type") == "function_call":
            self._stop_reason = "tool_use"
        self._current_block_open = False
        yield self._sse("content_block_stop", {
            "type": "content_block_stop", "index": self._current_block_index,
        })

    def _finish(self, event: dict, stop_reason: Optional[str]) -> Iterator[bytes]:
        if self._current_block_open:
            yield self._sse("content_block_stop", {
                "type": "content_block_stop", "index": self._current_block_index,
            })
            self._current_block_open = False
        response = event.get("response", {})
        usage = response.get("usage") or {}
        # The Responses API nests the cache and reasoning counts:
        # usage.input_tokens_details.cached_tokens and
        # usage.output_tokens_details.reasoning_tokens. Reading a flat
        # `cached_input_tokens` (which does not exist) recorded every Codex
        # request as fully uncached. And OpenAI's input_tokens INCLUDES the
        # cached part while Anthropic's excludes it, so it is split here —
        # the total Claude Code adds up for its context stays the same.
        in_details = usage.get("input_tokens_details") or {}
        out_details = usage.get("output_tokens_details") or {}
        total_input = int(usage.get("input_tokens", 0) or 0)
        cached = min(int(in_details.get("cached_tokens", 0) or 0), total_input)
        self.usage = TranslatedUsage(
            input_tokens=total_input - cached,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
            reasoning_tokens=int(out_details.get("reasoning_tokens", 0) or 0),
        )
        final_stop_reason = stop_reason or self._stop_reason or ("end_turn" if self._saw_any_output else "end_turn")
        yield self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": final_stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens,
                      "cache_read_input_tokens": self.usage.cache_read_input_tokens,
                      "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
                      # Not an Anthropic field; Claude Code ignores it, and the
                      # gateway's usage capture records it.
                      "reasoning_tokens": self.usage.reasoning_tokens},
        })
        yield self._sse("message_stop", {"type": "message_stop"})


def assemble_message_from_sse(chunks: Iterator[bytes]) -> dict:
    """Collapses the Anthropic SSE this module produces back into a single
    Messages API response body, for a client that asked for stream:false.

    Deliberately consumes the streaming output rather than translating
    OpenAI's events a second way: the upstream Responses call is always
    streamed, so this is the only place the two shapes could diverge, and
    reusing the same events means they cannot.

    Claude Code's auto-mode safety classifier issues exactly this kind of
    non-streaming request. Answering it with an SSE body makes the client
    treat the model as unavailable, which silently blocks tools that need a
    safety decision.
    """
    message: dict = {
        "id": "msg_openai_bridge", "type": "message", "role": "assistant",
        "model": "", "content": [], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    blocks: dict[int, dict] = {}
    partial_json: dict[int, list[str]] = {}
    started = False
    failure: Optional[dict] = None

    for raw in chunks:
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "error":
                # A stream that failed after it began. Kept, not ignored:
                # ignoring it returned a 200 with empty content, which the
                # client reports as "the model produced an empty response" —
                # a lie about what happened, and one that hid every upstream
                # failure behind it (Claude Code's own auto-compaction was
                # failing this way).
                failure = event.get("error") or {"type": "api_error", "message": "upstream stream failed"}
            elif kind == "message_start":
                started = True
                opened = event.get("message") or {}
                message["id"] = opened.get("id", message["id"])
                message["model"] = opened.get("model", "")
            elif kind == "content_block_start":
                index = event.get("index", 0)
                block = dict(event.get("content_block") or {})
                if block.get("type") == "text":
                    block.setdefault("text", "")
                blocks[index] = block
                partial_json[index] = []
            elif kind == "content_block_delta":
                index = event.get("index", 0)
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and index in blocks:
                    blocks[index]["text"] = blocks[index].get("text", "") + delta.get("text", "")
                elif delta.get("type") == "input_json_delta":
                    partial_json.setdefault(index, []).append(delta.get("partial_json", ""))
            elif kind == "message_delta":
                message["stop_reason"] = (event.get("delta") or {}).get("stop_reason", message["stop_reason"])
                message["usage"].update(event.get("usage") or {})

    if failure is not None or not started:
        # Anthropic's error body shape, so the caller can send it as the error
        # it is rather than as a successful answer with nothing in it.
        return {"type": "error",
                "error": failure or {"type": "api_error",
                                     "message": "upstream returned no response"}}

    for index in sorted(blocks):
        block = blocks[index]
        # Belt and braces with the streaming side: an empty text block is what
        # Anthropic rejects on the next request ("text content blocks must be
        # non-empty"), so it must not survive into a message either.
        if block.get("type") == "text" and not block.get("text"):
            continue
        if block.get("type") == "tool_use":
            raw_input = "".join(partial_json.get(index, []))
            try:
                block["input"] = json.loads(raw_input) if raw_input else {}
            except json.JSONDecodeError:
                # Never drop the call: an unparsable argument string is still
                # more useful to the client than a silently empty input.
                block["input"] = {"_raw": raw_input}
        message["content"].append(block)
    return message
