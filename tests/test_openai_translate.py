import json

from claude_unlimited.openai_models import OpenAIModelTarget
from claude_unlimited.openai_translate import ResponseTranslator, anthropic_request_to_openai

TARGET = OpenAIModelTarget("gpt-5.6-terra", "medium")


# ---- request translation ----

def test_simple_text_request_translates_model_instructions_and_input():
    body = {
        "model": "claude-sonnet-5",
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "Hello there"}],
    }
    out = anthropic_request_to_openai(body, TARGET)
    assert out["model"] == "gpt-5.6-terra"
    assert out["instructions"] == "You are a helpful assistant."
    assert out["reasoning"] == {"effort": "medium"}
    assert out["stream"] is True
    assert out["store"] is False
    assert out["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello there"}]}
    ]


def test_system_as_content_block_list_is_joined():
    body = {"system": [{"type": "text", "text": "Part one."}, {"type": "text", "text": "Part two."}],
            "messages": []}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["instructions"] == "Part one.\n\nPart two."


def test_no_system_omits_instructions_key():
    out = anthropic_request_to_openai({"messages": []}, TARGET)
    assert "instructions" not in out


def test_assistant_text_message_uses_output_text_not_input_text():
    body = {"messages": [{"role": "assistant", "content": "Sure, here you go."}]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Sure, here you go."}]}
    ]


def test_tool_use_block_becomes_a_function_call_item():
    body = {"messages": [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
        ],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Let me check."}]},
        {"type": "function_call", "call_id": "call_1", "name": "Bash", "arguments": json.dumps({"command": "ls"})},
    ]


def test_tool_result_block_becomes_a_function_call_output_item():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "total 0"}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "total 0"},
    ]


def test_tool_result_with_structured_content_blocks_joins_text_parts():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_2",
                      "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"][0]["output"] == "line one\nline two"


def test_base64_image_block_becomes_a_data_uri_input_image():
    body = {"messages": [{
        "role": "user",
        "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}],
    }]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"][0]["content"][0] == {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}


def test_tools_array_maps_name_description_and_input_schema_to_parameters():
    body = {"messages": [], "tools": [
        {"name": "Bash", "description": "Run a shell command",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    ]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["tools"] == [{
        "type": "function", "name": "Bash", "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        "strict": False,
    }]


def test_no_tools_omits_tools_key():
    out = anthropic_request_to_openai({"messages": []}, TARGET)
    assert "tools" not in out


def test_tool_choice_mapping():
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "auto"}}, TARGET)["tool_choice"] == "auto"
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "any"}}, TARGET)["tool_choice"] == "required"
    assert anthropic_request_to_openai({"messages": [], "tool_choice": {"type": "none"}}, TARGET)["tool_choice"] == "none"
    assert anthropic_request_to_openai(
        {"messages": [], "tool_choice": {"type": "tool", "name": "Bash"}}, TARGET)["tool_choice"] == "Bash"
    assert anthropic_request_to_openai({"messages": []}, TARGET)["tool_choice"] == "auto"


def test_unknown_content_block_type_is_skipped_not_fatal():
    body = {"messages": [{"role": "user", "content": [
        {"type": "some_future_block_type", "data": "whatever"},
        {"type": "text", "text": "still here"},
    ]}]}
    out = anthropic_request_to_openai(body, TARGET)
    assert out["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "still here"}]}
    ]


# ---- response translation ----

def _sse_bytes(chunks):
    return b"".join(chunks)


def test_text_only_turn_translates_to_a_full_anthropic_sse_sequence():
    translator = ResponseTranslator()
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 3, "cache_write_input_tokens": 0}}},
    ]
    out = b""
    for event in events:
        out += _sse_bytes(translator.feed(event))
    text = out.decode()

    assert "event: message_start" in text
    assert '"type": "text", "text": ""' in text
    assert '"text": "Hel"' in text
    assert '"text": "lo"' in text
    assert "event: content_block_stop" in text
    assert '"stop_reason": "end_turn"' in text
    assert "event: message_stop" in text
    assert translator.usage.input_tokens == 10
    assert translator.usage.output_tokens == 2
    assert translator.usage.cache_read_input_tokens == 3


def test_tool_call_turn_sets_tool_use_stop_reason():
    translator = ResponseTranslator()
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.6-terra"}},
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "call_1", "name": "Bash"}},
        {"type": "response.function_call_arguments.delta", "delta": '{"command"'},
        {"type": "response.function_call_arguments.delta", "delta": ':"ls"}'},
        {"type": "response.output_item.done", "item": {"type": "function_call"}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 3}}},
    ]
    out = b""
    for event in events:
        out += _sse_bytes(translator.feed(event))
    text = out.decode()

    assert '"type": "tool_use", "id": "call_1", "name": "Bash"' in text
    assert '"type": "input_json_delta", "partial_json": "{\\"command\\""' in text
    assert '"stop_reason": "tool_use"' in text


def test_response_failed_sets_error_stop_reason():
    translator = ResponseTranslator()
    list(translator.feed({"type": "response.created", "response": {}}))
    out = b"".join(translator.feed({"type": "response.failed", "response": {"usage": {}}}))
    assert b'"stop_reason": "error"' in out


def test_unrecognized_event_type_is_silently_ignored():
    translator = ResponseTranslator()
    # A future or unknown event type must never raise: the translator
    # deliberately avoids an exhaustive match.
    assert list(translator.feed({"type": "response.some_future_event"})) == []


def test_message_start_only_fires_once():
    translator = ResponseTranslator()
    first = b"".join(translator.feed({"type": "response.created", "response": {}}))
    second = b"".join(translator.feed({"type": "response.created", "response": {}}))
    assert b"message_start" in first
    assert second == b""
