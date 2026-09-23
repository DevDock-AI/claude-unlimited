"""Which upstream URLs a Profile may point at, in one place.

`base_url` is the UPSTREAM a Profile's credential is sent to, so plain http
means that credential crosses the network in the clear — refused. The one
exception is a host that cannot leave the local network: a model server on
this machine or on your own LAN (LM Studio, Ollama, llama.cpp, MLX servers
all speak plain http), where requiring TLS would mean a self-signed
certificate for no gain in who can read the traffic.

Two callers must agree on this or a Profile is accepted and then refused at
send time: profiles.py validates what is saved, upstream.py decides whether to
open an HTTPS or an HTTP connection. Hence one module.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def is_local_host(host: str | None) -> bool:
    """Whether this host is reachable only from this machine or its LAN.

    Names are never trusted — this is checked when a config is written and
    when a request is sent, and a name that resolves locally now can point
    anywhere later. Only literal addresses and `localhost` count.
    """
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def is_plaintext_allowed(url: str) -> bool:
    """Whether this URL may be sent over plain http."""
    return is_local_host(urlsplit(url).hostname)


class InvalidUpstreamURL(ValueError):
    """The URL is malformed, or plain http to somewhere off this network."""


def validate(url: str, *, allow_local_http: bool = True) -> None:
    """Raises InvalidUpstreamURL unless `url` is a usable upstream.

    `allow_local_http` is False for a codex-kind Profile: the Codex bridge
    only speaks HTTPS, so a plain-http Base URL there is refused when saved
    rather than failing on the first request."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or " " in url:
        raise InvalidUpstreamURL(
            "base_url must be an http:// or https:// URL, e.g. https://api.anthropic.com.")
    if parsed.scheme == "https":
        return
    if not allow_local_http:
        raise InvalidUpstreamURL(
            "base_url must start with https:// for a Codex profile. Plain http:// is accepted "
            "only for an API profile pointing at a local model server.")
    if not is_local_host(parsed.hostname):
        raise InvalidUpstreamURL(
            f"base_url must start with https:// for {parsed.hostname} — plain http would send "
            "this Profile's key across the network in the clear. http:// is accepted only for "
            "a local address (localhost, 127.0.0.1, or a private LAN address such as "
            "192.168.x.x / 10.x.x.x), where the request never leaves your network."
        )
