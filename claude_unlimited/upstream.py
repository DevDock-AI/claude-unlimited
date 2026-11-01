"""The one module allowed to open a real socket to Anthropic (or a gateway).

Kept separate from proxy.py's pure request-building so that tests never need
a real network connection: proxy.py is tested against fixtures, and this
module is exercised against a local fake HTTPS server, never api.anthropic.com.
"""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

from .proxy import UpstreamRequest


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body_chunks: Iterator[bytes]
    connection: http.client.HTTPConnection  # caller must close() after draining body_chunks


DEFAULT_TIMEOUT_SECONDS = 120
CHUNK_SIZE = 65536


# How long ONE resolved address may take to accept the connection before the
# next is tried. The request timeout is for waiting on the model; spent on a
# dead address it stalls every request. A `.local` name resolves to an IPv6
# link-local address first, and a server listening on IPv4 only leaves that
# attempt hanging for the whole timeout before IPv4 is even tried: a 20 s
# connection test, a 120 s wait on every proxied request.
CONNECT_TIMEOUT_SECONDS = 4.0


def _connect(address, timeout, source_address=None, *args):
    """socket.create_connection, with each address capped at
    CONNECT_TIMEOUT_SECONDS; the socket then gets the full `timeout` for
    the request itself."""
    host, port = address
    per_address = min(timeout, CONNECT_TIMEOUT_SECONDS) if timeout else CONNECT_TIMEOUT_SECONDS
    errors = []
    # IPv6 link-local (fe80::) last: it is what a `.local` name resolves to
    # first, and the address a model server is least likely to listen on.
    candidates = sorted(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM),
                        key=lambda info: info[0] == socket.AF_INET6
                        and str(info[4][0]).lower().startswith("fe80"))
    for family, kind, proto, _name, sockaddr in candidates:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(per_address)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    raise errors[0] if errors else OSError(f"could not resolve {host}")


def open_connection(scheme: str, host: str, port, timeout: float) -> http.client.HTTPConnection:
    """An HTTP(S) connection whose connect step moves on from a dead
    address quickly (see CONNECT_TIMEOUT_SECONDS)."""
    if scheme == "http":
        conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout)
    conn._create_connection = _connect
    return conn


def send(req: UpstreamRequest, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> UpstreamResponse:
    parts = urlsplit(req.url)
    plaintext = parts.scheme == "http" and getattr(req, "allow_http", False)
    if parts.scheme != "https" and not plaintext:
        raise ValueError(
            f"Refusing to send an upstream request over {parts.scheme!r} to {parts.hostname} — "
            "plain http is allowed only for an API profile's own Base URL (see net_scope, the "
            "same rule profiles.py validates a base_url with)."
        )

    # Plain http only for an API profile the user pointed at it (a local
    # model server, usually). Their key, their choice.
    conn = open_connection("http" if plaintext else "https", parts.hostname, parts.port, timeout)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    conn.request(req.method, path, body=req.body, headers=req.headers)
    resp = conn.getresponse()

    def _chunks() -> Iterator[bytes]:
        while True:
            # read1, not read: on a chunked SSE stream read(n) waits for n
            # bytes or EOF, batching events the client should see now.
            chunk = resp.read1(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    return UpstreamResponse(
        status=resp.status,
        headers=dict(resp.getheaders()),
        body_chunks=_chunks(),
        connection=conn,
    )
