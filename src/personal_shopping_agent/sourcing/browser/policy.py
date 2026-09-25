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


def _normalized(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(host.lower().rstrip(".") for host in hosts)


class NavigationPolicy:
    """Allow only explicitly configured public HTTPS destinations.

    Top-level pages must match an exact host. Resources a page loads itself may also come from a
    declared registrable platform domain. Redirects to a declared sign-in host stop collection so
    the user can sign in manually.
    """

    _BLOCKED_PATH_SEGMENTS = frozenset(
        {"checkout", "order", "orders", "pay", "payment", "payments", "trade"}
    )

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        subresource_domains: Iterable[str] = (),
        sign_in_hosts: Iterable[str] = (),
        resolver: AddressResolver = resolve_addresses,
    ) -> None:
        normalized_hosts = _normalized(allowed_hosts)
        if not normalized_hosts or "" in normalized_hosts:
            raise ValueError("allowed_hosts must contain at least one valid hostname")
        normalized_domains = _normalized(subresource_domains)
        if "" in normalized_domains or any("." not in domain for domain in normalized_domains):
            raise ValueError("subresource_domains must be registrable domain names")
        self._allowed_hosts = normalized_hosts
        self._subresource_domains = normalized_domains
        self._sign_in_hosts = _normalized(sign_in_hosts)
        self._resolver = resolver

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Return the exact host allowlist for diagnostics and tests."""

        return self._allowed_hosts

    async def validate(self, url: str) -> ValidatedURL:
        """Validate a top-level page; reject unsafe schemes, credentials, ports, paths, and IPs."""

        return await self._validate(url, self._allowed_page_host)

    async def validate_subresource(self, url: str) -> ValidatedURL:
        """Validate a resource loaded by an allowed page under the same network rules."""

        return await self._validate(url, self._allowed_subresource_host)

    def _allowed_page_host(self, hostname: str) -> None:
        if hostname in self._allowed_hosts:
            return
        if hostname in self._sign_in_hosts:
            raise NavigationPolicyError(
                "sign_in_required",
                "The platform requires a manual sign-in. Run `personal-shopping-agent login` "
                "for this platform on this computer, then retry.",
            )
        raise NavigationPolicyError(
            "host_not_allowed", "The destination host is not on the platform allowlist."
        )

    def _allowed_subresource_host(self, hostname: str) -> None:
        if hostname in self._allowed_hosts or any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self._subresource_domains
        ):
            return
        raise NavigationPolicyError(
            "host_not_allowed", "The resource host is not on the platform allowlist."
        )

    async def _validate(
        self,
        url: str,
        check_host: Callable[[str], None],
    ) -> ValidatedURL:
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
        check_host(hostname)

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
