"""Deterministic navigation policy applied before and during browser requests."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

AddressResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


class NavigationPolicyError(ValueError):
    """A sanitized rejection that is safe to expose to application code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """URL components accepted by the navigation policy."""

    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


async def resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a host without blocking the async MCP event loop."""

    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise NavigationPolicyError(
            "dns_resolution_failed", "The destination host could not be resolved."
        ) from error
    return tuple(sorted({str(record[4][0]) for record in records}))


class NavigationPolicy:
    """Allow only explicitly configured public HTTPS destinations."""

    _BLOCKED_PATH_SEGMENTS = frozenset(
        {"checkout", "order", "orders", "pay", "payment", "payments", "trade"}
    )

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        resolver: AddressResolver = resolve_addresses,
    ) -> None:
        normalized_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        if not normalized_hosts or "" in normalized_hosts:
            raise ValueError("allowed_hosts must contain at least one valid hostname")
        self._allowed_hosts = normalized_hosts
        self._resolver = resolver

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Return the exact host allowlist for diagnostics and tests."""

        return self._allowed_hosts

    async def validate(self, url: str) -> ValidatedURL:
        """Reject unsafe schemes, credentials, ports, paths, hosts, and IPs."""

        if len(url) > 2_048:
            raise NavigationPolicyError("url_too_long", "The destination URL is too long.")

        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            raise NavigationPolicyError("scheme_not_allowed", "Only HTTPS navigation is allowed.")
        if parts.username is not None or parts.password is not None:
            raise NavigationPolicyError(
                "embedded_credentials", "Credentials must not be embedded in a URL."
            )

        hostname = (parts.hostname or "").lower().rstrip(".")
        if hostname not in self._allowed_hosts:
            raise NavigationPolicyError(
                "host_not_allowed", "The destination host is not on the platform allowlist."
            )

        try:
            port = parts.port or 443
        except ValueError as error:
            raise NavigationPolicyError(
                "port_not_allowed", "The destination port is invalid."
            ) from error
        if port != 443:
            raise NavigationPolicyError(
                "port_not_allowed", "Only the standard HTTPS port is allowed."
            )

        path_segments = {
            unquote(segment).strip().lower() for segment in parts.path.split("/") if segment
        }
        if path_segments & self._BLOCKED_PATH_SEGMENTS:
            raise NavigationPolicyError(
                "transaction_path_blocked", "Checkout, order, and payment paths are not allowed."
            )

        addresses = await self._resolve(hostname, port)
        return ValidatedURL(url=url, hostname=hostname, port=port, addresses=addresses)

    async def _resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = await self._resolver(hostname, port)
        else:
            addresses = (str(address),)

        if not addresses:
            raise NavigationPolicyError(
                "dns_resolution_failed", "The destination host returned no network address."
            )
        try:
            is_public = all(ipaddress.ip_address(address).is_global for address in addresses)
        except ValueError as error:
            raise NavigationPolicyError(
                "invalid_network_address", "The destination resolved to an invalid network address."
            ) from error
        if not is_public:
            raise NavigationPolicyError(
                "private_network_blocked",
                "Private or non-public network destinations are not allowed.",
            )
        return addresses
