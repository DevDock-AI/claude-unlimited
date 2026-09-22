"""The one module allowed to open a real socket to Anthropic (or a gateway).

Kept separate from proxy.py's pure request-building so that tests never need
a real network connection: proxy.py is tested against fixtures, and this
module is exercised against a local fake HTTPS server, never api.anthropic.com.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from . import net_scope
from .proxy import UpstreamRequest


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body_chunks: Iterator[bytes]
    connection: http.client.HTTPConnection  # caller must close() after draining body_chunks


DEFAULT_TIMEOUT_SECONDS = 120
CHUNK_SIZE = 65536


def send(req: UpstreamRequest, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> UpstreamResponse:
    parts = urlsplit(req.url)
    plaintext = parts.scheme == "http" and net_scope.is_local_host(parts.hostname)
    if parts.scheme != "https" and not plaintext:
        raise ValueError(
            f"Refusing to send an upstream request over {parts.scheme!r} to {parts.hostname} — "
            "a credential would cross the network in the clear. Plain http is allowed only for "
            "a local address (see net_scope, the same rule profiles.py validates a base_url with)."
        )

    if plaintext:
        # A model server on this machine or on the LAN: the request never
        # leaves the local network, so there is nobody new to hide it from.
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    conn.request(req.method, path, body=req.body, headers=req.headers)
    resp = conn.getresponse()

    def _chunks() -> Iterator[bytes]:
        while True:
            chunk = resp.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    return UpstreamResponse(
        status=resp.status,
        headers=dict(resp.getheaders()),
        body_chunks=_chunks(),
        connection=conn,
    )
