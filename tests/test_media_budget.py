import json

from claude_unlimited.media_budget import TRIM_STEP_BYTES, fit_body

LIMIT = 20_000_000


def _image(data: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _body(*user_contents) -> bytes:
    messages = []
    for i, content in enumerate(user_contents):
        if i:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        messages.append({"role": "user", "content": content})
    return json.dumps({"model": "m", "messages": messages}).encode()


def test_body_under_the_limit_is_returned_untouched():
    body = _body([_image("A" * 1000)], "hi")
    assert fit_body(body, LIMIT) is body


def test_oldest_media_trimmed_first_text_and_latest_message_untouched():
    body = _body(
        [_image("A" * 9_000_000), {"type": "text", "text": "first"}],
        [{"type": "tool_result", "tool_use_id": "t", "content": [_image("B" * 9_000_000)]}],
        [_image("C" * 4_000_000)],
    )
    out = fit_body(body, LIMIT)
    assert len(out) <= LIMIT
    msgs = json.loads(out)["messages"]
    assert msgs[0]["content"][0]["type"] == "text"
    assert "no longer visible" in msgs[0]["content"][0]["text"]
    assert msgs[0]["content"][1]["text"] == "first"
    assert msgs[2]["content"][0]["content"][0]["type"] == "image"
    assert msgs[4]["content"][0]["source"]["data"] == "C" * 4_000_000


def test_documents_are_trimmed_and_their_cache_control_kept():
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": "D" * 21_000_000},
           "cache_control": {"type": "ephemeral"}}
    block = json.loads(fit_body(_body([doc], "hi"), LIMIT))["messages"][0]["content"][0]
    assert block["type"] == "text" and "document" in block["text"]
    assert block["cache_control"] == {"type": "ephemeral"}


def test_cut_off_moves_in_steps_so_small_growth_keeps_the_same_prefix():
    images = [[_image(chr(65 + i) * 1_000_000)] for i in range(25)]
    first = json.loads(fit_body(_body(*images, "a"), LIMIT))
    second = json.loads(fit_body(_body(*images, "a" * (TRIM_STEP_BYTES // 20)), LIMIT))
    assert first["messages"][:-1] == second["messages"][:-1]


def test_latest_message_alone_too_large_is_left_for_the_size_check():
    body = _body("hi", [_image("C" * 21_000_000)])
    assert fit_body(body, LIMIT) is body


def test_non_json_and_single_message_bodies_pass_through():
    assert fit_body(b"x" * 21_000_000, LIMIT) == b"x" * 21_000_000
    body = _body([_image("A" * 21_000_000)])
    assert fit_body(body, LIMIT) is body
