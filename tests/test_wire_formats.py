"""The translation boundary: which shape an endpoint speaks.

These exist because wire_formats.py shipped with none, and because the
no-op property of the refactor that introduced it is only meaningful if it
is asserted somewhere.
"""
import pytest

from claude_unlimited import wire_formats as wf
from claude_unlimited.openai_translate import ResponseTranslator, anthropic_request_to_openai


def test_an_unrecorded_format_keeps_the_pre_existing_behaviour():
    # Every codex Profile predates formats being named. None of them may
    # change behaviour because this module now exists.
    fmt = wf.get(None)
    assert fmt.name == wf.OPENAI_RESPONSES
    assert fmt.endpoint_path == "/responses"
    assert fmt.to_provider is anthropic_request_to_openai
    assert fmt.response_stream is ResponseTranslator
    assert fmt.uses_model_ladder is True


def test_an_unknown_format_raises_rather_than_defaulting():
    # Silently defaulting would send a request in the wrong shape to whatever
    # endpoint the Profile points at, surfacing as a confusing provider error
    # instead of the configuration mistake it is.
    with pytest.raises(wf.UnsupportedWireFormat):
        wf.get("nonsense")


def test_anthropic_messages_is_not_translatable_here():
    # It is proxied byte-for-byte elsewhere; asking this module to translate
    # it is a bug, not a fallback.
    with pytest.raises(wf.UnsupportedWireFormat):
        wf.get(wf.ANTHROPIC_MESSAGES)


def test_a_named_but_unimplemented_format_raises():
    # The constant exists before the implementation does; get() must not
    # pretend it works.
    with pytest.raises(wf.UnsupportedWireFormat):
        wf.get(wf.OPENAI_CHAT_COMPLETIONS)


def test_response_streams_are_per_response_not_shared():
    # Translators are stateful; a shared instance would interleave two
    # concurrent responses into each other.
    fmt = wf.get(None)
    assert fmt.response_stream() is not fmt.response_stream()


def test_only_codex_served_formats_walk_the_model_ladder():
    # The ladder describes the Codex lineup. Applying it to a local server,
    # which serves arbitrary model names, would substitute nonsense.
    for name, fmt in wf.FORMATS.items():
        if fmt.uses_model_ladder:
            assert name == wf.OPENAI_RESPONSES
