"""The capacity guard's numbers (docs/adr/0009): the hardcoded GPT window
table, the per-request budget derived from it, and the byte-based input
estimate. Pure; nothing here reads a file, a clock or the network — the
table's FRESHNESS against the Codex CLI's cache is scripts/check_gpt_windows.py,
deliberately outside pytest."""

import base64
import json

import pytest

import claude_unlimited.gpt_windows as gw
from claude_unlimited import openai_models


# --- the table's shape -----------------------------------------------------

def test_every_ladder_rung_and_default_target_has_a_subscription_row():
    """The ids a codex Profile can actually send to (openai_models' targets
    and its fallback ladder) must all be KNOWN, not assumed."""
    ids = set(openai_models.model_ladder(catalogue=None))
    ids |= {t.model for t in openai_models._MODEL_MAP.values()}
    ids |= {t.model for _, t in openai_models._FAMILY_FALLBACKS}
    ids.add(openai_models._DEFAULT_TARGET.model)
    missing = {i for i in ids if i not in gw.CODEX_BACKEND_WINDOWS}
    assert not missing, f"add these to gpt_windows.CODEX_BACKEND_WINDOWS: {missing}"


def test_the_subscription_rows_are_window_then_ceiling():
    for model, (window, ceiling) in gw.CODEX_BACKEND_WINDOWS.items():
        assert 0 < window <= ceiling, model
        # Budgeting past `window` walks a subscription into the higher-usage
        # band (D3); the ceiling is recorded for the freshness check only.
        assert gw.window_for(model).window == window


def test_budget_is_the_cli_headroom_minus_the_output_reserve():
    assert gw.budget_for(272_000) == 258_400 - 32_000 == 226_400
    assert gw.MIN_BUDGET == 226_400
    assert gw.budget_for(0) == 0


# --- backends --------------------------------------------------------------

def test_subscription_profiles_get_the_chatgpt_backend_window():
    info = gw.window_for("gpt-5.6-terra", auth_mode="chatgpt_subscription")
    assert (info.backend, info.window, info.budget, info.assumed) == \
        (gw.BACKEND_SUBSCRIPTION, 272_000, 226_400, False)


def test_api_key_profiles_get_the_public_api_window():
    info = gw.window_for("gpt-6-astra", auth_mode="api_key")
    assert (info.backend, info.window, info.assumed) == (gw.BACKEND_OPENAI_API, 922_000, False)
    assert info.budget == 922_000 * 95 // 100 - 32_000


def test_a_custom_base_url_is_an_unknown_backend_at_the_floor():
    info = gw.window_for("gpt-6-astra", auth_mode="api_key", base_url="http://localhost:11434/v1")
    assert (info.backend, info.window, info.assumed) == (gw.BACKEND_UNKNOWN, 272_000, True)
    explicit = gw.window_for("gpt-6-astra", auth_mode="api_key", base_url="https://api.openai.com/v1")
    assert explicit.backend == gw.BACKEND_OPENAI_API and not explicit.assumed


@pytest.mark.parametrize("model", ["gpt-99-nova", "", None, "  "])
def test_an_unknown_id_is_assumed_at_the_floor_never_excluded(model):
    """D5: every slug the ChatGPT backend has ever listed had exactly 272K, so
    that is the honest floor; the guess is flagged, the account is not idled."""
    info = gw.window_for(model, auth_mode="chatgpt_subscription")
    assert info.window == gw.UNKNOWN_WINDOW and info.budget == gw.MIN_BUDGET
    assert info.assumed is True


def test_ids_are_matched_case_insensitively():
    assert gw.window_for("GPT-5.6-Sol").assumed is False


# --- the estimate ----------------------------------------------------------

def _body(text: str, images: int = 0, image_bytes: int = 0) -> tuple[bytes, dict]:
    content = [{"type": "text", "text": text}]
    for _ in range(images):
        data = base64.b64encode(b"\x89PNG" * (image_bytes // 4 + 1)).decode()[:image_bytes]
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": data}})
    parsed = {"model": "claude-opus-5", "messages": [{"role": "user", "content": content}]}
    return json.dumps(parsed).encode(), parsed


def test_text_is_estimated_at_three_bytes_per_token_plus_the_envelope():
    body, parsed = _body("x" * 3_000)
    assert gw.estimate_input_tokens(body, parsed) == -(-len(body) // 3) + gw.FIXED_OVERHEAD


def test_an_image_costs_its_flat_tile_charge_not_its_base64():
    """A 1 MB screenshot is ~1.4 MB of base64 — 460K "tokens" at /3, which
    would bar codex from any conversation holding one."""
    plain, plain_parsed = _body("hello")
    with_image, parsed = _body("hello", images=1, image_bytes=1_400_000)
    assert len(with_image) > 1_400_000
    estimate = gw.estimate_input_tokens(with_image, parsed)
    assert estimate < gw.MIN_BUDGET
    # Exactly: the non-image bytes at /3, one flat image charge, the envelope.
    assert estimate == -(-(len(with_image) - 1_400_000) // 3) + gw.IMAGE_TOKENS + gw.FIXED_OVERHEAD
    # And within a couple of hundred tokens of the same body without the image.
    assert estimate - gw.estimate_input_tokens(plain, plain_parsed) < gw.IMAGE_TOKENS + 200


def test_a_tiny_image_never_counts_for_more_than_its_bytes():
    """Keeps the estimate monotone in body size, which is what makes the
    stage-1 byte floor exact."""
    body, parsed = _body("hello", images=1, image_bytes=300)
    assert gw.estimate_input_tokens(body, parsed) <= -(-len(body) // 3) + gw.FIXED_OVERHEAD


def test_an_unparsable_body_is_sized_from_its_bytes_alone():
    """Nothing to subtract: the estimate can only be HIGHER, which only ever
    excludes codex — never an Anthropic account, which is never sized."""
    body, _ = _body("hello", images=1, image_bytes=900_000)
    assert gw.estimate_input_tokens(body, None) > gw.estimate_input_tokens(body, json.loads(body))
    assert gw.estimate_input_tokens(body, None) == -(-len(body) // 3) + gw.FIXED_OVERHEAD


@pytest.mark.parametrize("parsed", [
    {}, {"messages": "nope"}, {"messages": [None, 3, {"content": "text"}]},
    {"messages": [{"content": [{"type": "image"}, {"type": "image", "source": "x"},
                               {"type": "image", "source": {"type": "url", "url": "http://x"}}]}]},
])
def test_odd_shapes_never_raise(parsed):
    body = json.dumps(parsed).encode()
    assert gw.estimate_input_tokens(body, parsed) >= gw.FIXED_OVERHEAD


def test_the_byte_floor_is_exact():
    """Any body under CERTAINLY_FITS_BYTES estimates under the smallest
    budget, so the gateway is right to skip the parse there."""
    body = b"x" * (gw.CERTAINLY_FITS_BYTES - 1)
    assert gw.estimate_input_tokens(body, None) <= gw.MIN_BUDGET
    assert gw.CERTAINLY_FITS_BYTES == (gw.MIN_BUDGET - gw.FIXED_OVERHEAD) * 3


def test_the_estimate_is_monotone_in_body_size():
    small, sp = _body("a" * 1_000)
    large, lp = _body("a" * 2_000)
    assert gw.estimate_input_tokens(small, sp) < gw.estimate_input_tokens(large, lp)


# --- the client-facing wording ----------------------------------------------

def test_the_prompt_too_long_message_is_byte_exact_for_claude_codes_parser():
    """2.1.278: /prompt is too long[^0-9]*(\\d+)\\s*tokens?\\s*>\\s*(\\d+)/i,
    and the rendered text must START with "Prompt is too long"."""
    import re
    message = gw.prompt_too_long_message(300_123, 226_400)
    assert message == "prompt is too long: 300123 tokens > 226400 maximum"
    match = re.search(r"prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)", message, re.I)
    assert match and match.groups() == ("300123", "226400")
    assert message.lower().startswith("prompt is too long")


# --- the per-profile summary the preview and launch note use -----------------

def test_codex_profile_windows_reports_the_tightest_budget_across_targets():
    from claude_unlimited.config import Profile

    p = Profile(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription")
    summary = openai_models.codex_profile_windows(p, parity=None)
    assert summary["window"] == 272_000 and summary["budget"] == 226_400
    assert summary["assumed"] is False
    assert "gpt-5.6-terra" in summary["models"]


def test_codex_profile_windows_flags_an_override_it_has_not_met():
    from claude_unlimited.config import Profile

    p = Profile(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription",
                codex_model="gpt-99-nova")
    summary = openai_models.codex_profile_windows(p, parity=None)
    assert summary["models"] == ["gpt-99-nova"] and summary["assumed"] is True


def test_codex_profile_windows_survives_a_broken_parity_list():
    from claude_unlimited.config import Profile

    p = Profile(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription")
    summary = openai_models.codex_profile_windows(p, parity=object())
    assert summary["budget"] == 226_400
