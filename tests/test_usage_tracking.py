import json

import pytest

import claude_unlimited.usage_tracking as usage_tracking

# Shaped exactly like a captured SSE stream from a Haiku request through the
# proxy; see usage_tracking.py's module docstring.
REAL_SHAPED_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001","id":"msg_1",'
    b'"type":"message","role":"assistant","content":[],"stop_reason":null,"stop_sequence":null,'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":1}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: ping\n'
    b'data: {"type": "ping"}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hey"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"! How\'s it going"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens","stop_sequence":null},'
    b'"usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":10}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


def _chunks_of(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def test_tee_yields_every_byte_unchanged_regardless_of_chunking():
    for chunk_size in (1, 3, 7, 64, 4096):
        capture = usage_tracking.UsageCapture()
        forwarded = b"".join(capture.wrap(_chunks_of(REAL_SHAPED_SSE, chunk_size), "text/event-stream"))
        assert forwarded == REAL_SHAPED_SSE, f"tee altered bytes at chunk_size={chunk_size}"


def test_sse_captures_model_and_final_usage_single_chunk():
    capture = usage_tracking.UsageCapture()
    list(capture.wrap(iter([REAL_SHAPED_SSE]), "text/event-stream"))
    assert capture.model == "claude-haiku-4-5-20251001"
    # message_delta's usage (output_tokens=10) must win over message_start's provisional (1).
    assert capture.usage == {"input_tokens": 8, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 10}


def test_sse_captures_correctly_when_split_across_arbitrary_chunk_boundaries():
    for chunk_size in (1, 2, 5, 13, 37):
        capture = usage_tracking.UsageCapture()
        list(capture.wrap(_chunks_of(REAL_SHAPED_SSE, chunk_size), "text/event-stream"))
        assert capture.model == "claude-haiku-4-5-20251001", f"failed at chunk_size={chunk_size}"
        assert capture.usage["output_tokens"] == 10, f"failed at chunk_size={chunk_size}"


def test_sse_with_only_message_start_has_provisional_usage():
    only_start = REAL_SHAPED_SSE.split(b"event: content_block_start")[0]
    capture = usage_tracking.UsageCapture()
    list(capture.wrap(iter([only_start]), "text/event-stream"))
    assert capture.model == "claude-haiku-4-5-20251001"
    assert capture.usage["output_tokens"] == 1  # provisional, never overwritten


def test_non_streaming_json_body_captures_model_and_usage():
    body = json.dumps({
        "id": "msg_1", "model": "claude-sonnet-5", "role": "assistant",
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }).encode("utf-8")
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(_chunks_of(body, 9), "application/json"))
    assert forwarded == body
    assert capture.model == "claude-sonnet-5"
    assert capture.usage == {"input_tokens": 42, "output_tokens": 7}


def test_malformed_sse_never_raises_and_still_forwards_bytes():
    garbage = b"event: message_start\ndata: {not valid json at all\n\nmore garbage bytes here\n\n"
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([garbage]), "text/event-stream"))
    assert forwarded == garbage  # forwarding survives even though parsing failed
    assert capture.model is None
    assert capture.usage is None


def test_malformed_json_body_never_raises():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([b"not json"]), "application/json"))
    assert forwarded == b"not json"
    assert capture.model is None


def test_empty_response_body_leaves_capture_empty():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([]), "text/event-stream"))
    assert forwarded == b""
    assert capture.model is None
    assert capture.usage is None


def test_oversized_json_body_is_capped_not_buffered_forever():
    huge_chunk = b"x" * 6_000_000
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([huge_chunk]), "application/json"))
    assert forwarded == huge_chunk  # still forwarded in full
    assert capture._json_buffer_capped is True
    assert capture.model is None


def test_content_type_missing_defaults_to_non_streaming_path_without_raising():
    capture = usage_tracking.UsageCapture()
    forwarded = b"".join(capture.wrap(iter([b"whatever"]), None))
    assert forwarded == b"whatever"
