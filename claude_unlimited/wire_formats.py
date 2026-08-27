"""What shape an endpoint speaks, and how to translate to and from it.

Claude Code always speaks Anthropic Messages. What sits on the other side does
not: OpenAI's Codex backend speaks the Responses API, while LM Studio, Ollama
and most self-hosted servers speak OpenAI Chat Completions. Those differ in
both request and response shape, so pointing a Profile at a local server is not
a matter of changing the base URL — it is a different wire format.

This module owns exactly that: the request/response translation boundary, and
the path a format is served at. It deliberately owns nothing else. Credentials,
rotation, threshold logic and usage capture stay where they are, in gateway.py
and openai_bridge.py — that is the code a live session depends on, and none of
the providers on the roadmap need it abstracted.

Anthropic Messages is deliberately NOT in FORMATS. A Profile speaking it is
proxied byte-for-byte by proxy.py/upstream.py with no translation at all, so
routing it through a translation interface would add a layer that does nothing.
The name exists as a constant so a Profile can record what it speaks, but
asking this module to translate it is a bug, and get() says so rather than
quietly handing back a different format.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional, Protocol

from .openai_models import OpenAIModelTarget
from .openai_translate import ResponseTranslator, anthropic_request_to_openai

ANTHROPIC_MESSAGES = "anthropic_messages"
OPENAI_RESPONSES = "openai_responses"
OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"


class ResponseStream(Protocol):
    """Consumes provider SSE events and yields Anthropic-shaped SSE bytes."""

    def feed(self, event: dict) -> Iterator[bytes]:
        ...


class WireFormat:
    """One endpoint shape.

    `endpoint_path` is appended to a Profile's base URL. `to_provider`
    translates an already-decoded Anthropic request body. `response_stream`
    returns a fresh translator per response — they are stateful, so a shared
    instance would interleave two concurrent responses.
    """

    def __init__(self, name: str, endpoint_path: str,
                 to_provider: Callable[[dict, OpenAIModelTarget], dict],
                 response_stream: Callable[[], ResponseStream],
                 *, uses_model_ladder: bool = False) -> None:
        self.name = name
        self.endpoint_path = endpoint_path
        self.to_provider = to_provider
        self.response_stream = response_stream
        # Whether a rejected model should walk openai_models.fallback_models().
        # True only for formats served by the Codex backend, whose lineup that
        # ladder describes. A local server serves arbitrary model names, and
        # substituting a Codex model for one of those would be nonsense.
        self.uses_model_ladder = uses_model_ladder


FORMATS: dict[str, WireFormat] = {
    OPENAI_RESPONSES: WireFormat(
        name=OPENAI_RESPONSES,
        endpoint_path="/responses",
        to_provider=anthropic_request_to_openai,
        response_stream=ResponseTranslator,
        uses_model_ladder=True,
    ),
}


class UnsupportedWireFormat(ValueError):
    """A Profile names a format this build cannot translate."""


def get(name: Optional[str]) -> WireFormat:
    """The named format.

    `None` means "not recorded", which is every codex Profile that predates
    formats being named — those keep behaving exactly as they did, on the
    Responses API.

    An unknown or untranslatable name raises. Returning a default here would
    silently send a request in the wrong shape to whatever endpoint the Profile
    points at, and the failure would surface as a confusing provider error
    rather than as the configuration mistake it is."""
    if name is None:
        return FORMATS[OPENAI_RESPONSES]
    try:
        return FORMATS[name]
    except KeyError:
        raise UnsupportedWireFormat(
            f"{name!r} is not a wire format this build can translate; "
            f"known formats: {sorted(FORMATS)}") from None
