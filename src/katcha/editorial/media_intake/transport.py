"""HTTPS transport whose TCP sockets use only the public DNS snapshot checked here."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from httpcore._backends.sync import SyncStream

from katcha.editorial.media_intake.contracts import ProviderPermissionV1


def public_addresses(host: str) -> list[str]:
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Media host could not be resolved") from exc
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    else:
        addresses = [direct]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Media host resolves to a non-public address")
    return list(dict.fromkeys(str(address) for address in addresses))


class PublicDestinationBackend(httpcore.SyncBackend):
    def __init__(self, permission: ProviderPermissionV1) -> None:
        self.permission = permission

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        if port != 443 or not self.permission.allows_host(host):
            raise ValueError("Media destination is not allowlisted HTTPS")
        if not self.permission.allows("download"):
            raise ValueError("Provider-specific download permission is absent or expired")
        addresses = public_addresses(host)
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error: OSError | None = None
        for address in addresses:
            family = (
                socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            )
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    raise TimeoutError("Connection deadline expired")
                sock.settimeout(remaining)
                if local_address is not None:
                    sock.bind((local_address, 0))
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Direct numeric socket.connect never calls getaddrinfo again.
                sock.connect((address, port))
                return SyncStream(sock)
            except OSError as exc:
                sock.close()
                last_error = exc
        if isinstance(last_error, socket.timeout):
            raise httpcore.ConnectTimeout(str(last_error)) from last_error
        raise httpcore.ConnectError(str(last_error)) from last_error


class PublicDestinationTransport(httpx.HTTPTransport):
    def __init__(self, permission: ProviderPermissionV1) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            network_backend=PublicDestinationBackend(permission),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
        )
        self.permission = permission

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.port not in {None, 443}
            or not self.permission.allows_host(request.url.host)
            or "sni_hostname" in request.extensions
        ):
            raise ValueError("Media destination must use the original allowlisted HTTPS hostname")
        # HTTPcore retains this hostname for Host, TLS SNI and certificate verification.
        return super().handle_request(request)
