"""Per-request token/model usage capture from Anthropic response bodies.

An Anthropic SSE stream has this structure (content elided):

    event: message_start
    data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001",...,
           "usage":{"input_tokens":8,"cache_creation_input_tokens":0,
           "cache_read_input_tokens":0,"output_tokens":1,...}}}

    event: content_block_start / content_block_delta / content_block_stop
    (the actual text — never read by this module)

    event: message_delta
    data: {"type":"message_delta","delta":{"stop_reason":"max_tokens",...},
           "usage":{"input_tokens":8,"cache_creation_input_tokens":0,
           "cache_read_input_tokens":0,"output_tokens":10}}

    event: message_stop

`message_start` carries the model name and a provisional usage snapshot
(output_tokens is a placeholder, always small); `message_delta` carries the
authoritative final usage (all four fields, self-contained — not a diff).
This module reads only `type`, `message.model`, and `usage` — never message
content — and only from its own separate copy of the bytes.

Safety invariant: UsageCapture.wrap() is a strict tee. It yields every chunk
exactly as received, in order and unmodified, and only inspects a separate
copy of the bytes. Every parsing step is guarded so a failure here can never
raise past this module or alter what the client receives; the worst case is
that one request's usage isn't captured.
"""

from __future__ import annotations

import json
from typing import Iterator, Optional

_JSON_BODY_CAP_BYTES = 5_000_000  # give up buffering non-streaming bodies larger than this


class UsageCapture:
    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.usage: Optional[dict] = None
        self._sse_buffer = b""
        self._json_buffer = b""
        self._json_buffer_capped = False

    def wrap(self, chunks: Iterator[bytes], content_type: Optional[str]) -> Iterator[bytes]:
        is_sse = "text/event-stream" in (content_type or "")
        for chunk in chunks:
            try:
                if is_sse:
                    self._feed_sse(chunk)
                else:
                    self._feed_json(chunk)
            except Exception:
                pass  # capture must never affect forwarding
            yield chunk
        if not is_sse:
            try:
                self._finalize_json()
            except Exception:
                pass

    # ---- SSE (streaming) ----

    def _feed_sse(self, chunk: bytes) -> None:
        self._sse_buffer += chunk
        while b"\n\n" in self._sse_buffer:
            event_block, self._sse_buffer = self._sse_buffer.split(b"\n\n", 1)
            self._parse_sse_event(event_block)

    def _parse_sse_event(self, block: bytes) -> None:
        data_lines = [line[len(b"data:"):].strip() for line in block.split(b"\n") if line.startswith(b"data:")]
        if not data_lines:
            return
        payload = json.loads(b"\n".join(data_lines).decode("utf-8"))
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message") or {}
            if message.get("model"):
                self.model = message["model"]
            usage = message.get("usage")
            if isinstance(usage, dict) and self.usage is None:
                self.usage = usage  # provisional; message_delta overwrites with the final count
        elif event_type == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self.usage = usage  # authoritative, self-contained (not a diff)

    # ---- plain JSON (non-streaming) ----

    def _feed_json(self, chunk: bytes) -> None:
        if self._json_buffer_capped:
            return
        self._json_buffer += chunk
        if len(self._json_buffer) > _JSON_BODY_CAP_BYTES:
            self._json_buffer_capped = True
            self._json_buffer = b""

    def _finalize_json(self) -> None:
        if self._json_buffer_capped or not self._json_buffer:
            return
        payload = json.loads(self._json_buffer.decode("utf-8"))
        if payload.get("model"):
            self.model = payload["model"]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
