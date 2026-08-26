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
from typing import Iterator, Optional

from .openai_models import OpenAIModelTarget


# ---- Anthropic request -> OpenAI Responses API request ----

def anthropic_request_to_openai(body: dict, target: OpenAIModelTarget) -> dict:
    """Translates one already-JSON-decoded Anthropic /v1/messages request
    body into an OpenAI Responses API request body.

    Every Claude Code request carries the full conversation, since the
    Anthropic API requires history to be re-sent each turn. It is translated
    faithfully into OpenAI's `input` item list, never summarized or
    truncated."""
    instructions = _extract_system_text(body.get("system"))
    input_items: list[dict] = []
    for message in body.get("messages", []):
        input_items.extend(_message_to_input_items(message))

    openai_body: dict = {
        "model": target.model,
        "input": input_items,
        "stream": True,
        "store": False,
        "tool_choice": _map_tool_choice(body.get("tool_choice")),
        "parallel_tool_calls": True,
        "reasoning": {"effort": target.reasoning_effort},
    }
    if instructions:
        openai_body["instructions"] = instructions
    tools = _map_tools(body.get("tools"))
    if tools:
        openai_body["tools"] = tools
    return openai_body


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


def _map_tool_choice(anthropic_tool_choice) -> str:
    if not isinstance(anthropic_tool_choice, dict):
        return "auto"
    kind = anthropic_tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and anthropic_tool_choice.get("name"):
        return anthropic_tool_choice["name"]
    return "auto"


def _map_tools(anthropic_tools) -> list[dict]:
    if not isinstance(anthropic_tools, list):
        return []
    tools = []
    for t in anthropic_tools:
        if not isinstance(t, dict) or not t.get("name"):
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
            self._current_block_type = "text"
            self._current_block_index += 1
            self._current_block_open = True
            yield self._sse("content_block_start", {
                "type": "content_block_start", "index": self._current_block_index,
                "content_block": {"type": "text", "text": ""},
            })
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
            yield self._sse("content_block_start", {
                "type": "content_block_start", "index": self._current_block_index,
                "content_block": {"type": "tool_use", "id": self._pending_tool_call_id,
                                   "name": self._pending_tool_name, "input": {}},
            })
        # web_search, reasoning and other item types open no Anthropic
        # block.

    def _text_delta(self, event: dict) -> Iterator[bytes]:
        delta = event.get("delta", "")
        if not delta or self._current_block_index < 0:
            return
        self._saw_any_output = True
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
        self.usage = TranslatedUsage(
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            cache_read_input_tokens=usage.get("cached_input_tokens", 0) or 0,
            cache_creation_input_tokens=usage.get("cache_write_input_tokens", 0) or 0,
        )
        final_stop_reason = stop_reason or self._stop_reason or ("end_turn" if self._saw_any_output else "end_turn")
        yield self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": final_stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens},
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

    for raw in chunks:
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "message_start":
                started = event.get("message") or {}
                message["id"] = started.get("id", message["id"])
                message["model"] = started.get("model", "")
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

    for index in sorted(blocks):
        block = blocks[index]
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
