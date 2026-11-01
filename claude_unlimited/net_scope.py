"""Which upstream URLs a Profile may point at, in one place.

An API profile may use plain http:// to any address. That is the user's
call: a model server on this machine, on the LAN, under a .local name, or
behind their own tunnel usually speaks plain http, and the key they give
it is theirs. Every other Base URL (Codex) must be https://, since those
carry subscription credentials to a fixed provider.

Two callers must agree on this or a Profile is accepted and then refused at
send time: profiles.py validates what is saved, and proxy.py marks the
request so upstream.py opens an HTTP or an HTTPS connection. Hence one
module.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class InvalidUpstreamURL(ValueError):
    """The URL is malformed, or plain http where only https is allowed."""


def validate(url: str, *, allow_http: bool = True) -> None:
    """Raises InvalidUpstreamURL unless `url` is a usable upstream.

    `allow_http` is True for an API profile only."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or " " in url:
        raise InvalidUpstreamURL(
            "base_url must be an http:// or https:// URL, e.g. https://api.anthropic.com.")
    if parsed.scheme == "http" and not allow_http:
        raise InvalidUpstreamURL(
            "base_url must start with https:// for a Codex profile. Plain http:// is accepted "
            "only for an API profile.")
