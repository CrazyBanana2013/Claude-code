"""Lässt nur Anfragen aus erlaubten Netzen (LAN, Tailscale, localhost) durch."""

from __future__ import annotations

import ipaddress
import json
from typing import Iterable

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(networks: Iterable[str]) -> list[IPNetwork]:
    parsed: list[IPNetwork] = [ipaddress.ip_network(n, strict=False) for n in networks]
    # IPv6-Loopback gehört logisch zu 127.0.0.0/8.
    if any(n.version == 4 and ipaddress.ip_address("127.0.0.1") in n for n in parsed):
        parsed.append(ipaddress.ip_network("::1/128"))
    return parsed


def is_allowed(host: str | None, networks: list[IPNetwork]) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in networks)


class NetGuardMiddleware:
    """ASGI-Middleware: 403 für Quell-IPs außerhalb der erlaubten Netze.

    Es wird bewusst nur die TCP-Quelladresse geprüft, keine Header wie X-Forwarded-For,
    da kein Reverse-Proxy vorgesehen ist und Header fälschbar sind.
    """

    def __init__(self, app, networks: Iterable[str]) -> None:
        self.app = app
        self.networks = parse_networks(networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else None
        if is_allowed(host, self.networks):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "Zugriff nur aus LAN oder Tailscale erlaubt."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
