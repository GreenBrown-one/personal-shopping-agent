"""Unit tests for the public-network-only browser navigation policy."""

import asyncio
import socket
from collections.abc import Awaitable, Coroutine
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_shopping_agent.sourcing.browser import NavigationPolicy, NavigationPolicyError
from personal_shopping_agent.sourcing.browser.policy import resolve_addresses

PUBLIC_ADDRESSES = ("8.8.8.8", "2001:4860:4860::8888")


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    """Run one async policy operation without adding a pytest event-loop plugin."""

    return asyncio.run(awaitable)


def public_resolver(_hostname: str, _port: int) -> Awaitable[tuple[str, ...]]:
    async def _resolve() -> tuple[str, ...]:
        return PUBLIC_ADDRESSES

    return _resolve()


def assert_policy_error(policy: NavigationPolicy, url: str, code: str) -> None:
    with pytest.raises(NavigationPolicyError) as captured:
        run(policy.validate(url))
    assert captured.value.code == code


def test_policy_accepts_only_allowlisted_public_https_urls() -> None:
    policy = NavigationPolicy({"SHOP.EXAMPLE."}, resolver=public_resolver)

    validated = run(policy.validate("https://shop.example/products?q=phone"))

    assert policy.allowed_hosts == frozenset({"shop.example"})
    assert validated.hostname == "shop.example"
    assert validated.port == 443
    assert validated.addresses == PUBLIC_ADDRESSES


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("x" * 2_049, "url_too_long"),
        ("http://shop.example/products", "scheme_not_allowed"),
        ("https://user:secret@shop.example/products", "embedded_credentials"),
        ("https://other.example/products", "host_not_allowed"),
        ("https:///missing-host", "host_not_allowed"),
        ("https://shop.example:444/products", "port_not_allowed"),
        ("https://shop.example:invalid/products", "port_not_allowed"),
        ("https://shop.example/%70ay", "transaction_path_blocked"),
        ("https://shop.example/orders/1", "transaction_path_blocked"),
    ],
)
def test_policy_rejects_unsafe_url_components(url: str, code: str) -> None:
    assert_policy_error(NavigationPolicy({"shop.example"}, resolver=public_resolver), url, code)


@pytest.mark.parametrize("hosts", [(), ("", "shop.example")])
def test_policy_requires_a_nonempty_allowlist(hosts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="allowed_hosts"):
        NavigationPolicy(hosts)


@pytest.mark.parametrize(
    ("addresses", "code"),
    [
        ((), "dns_resolution_failed"),
        (("not-an-ip",), "invalid_network_address"),
        (("8.8.8.8", "127.0.0.1"), "private_network_blocked"),
    ],
)
def test_policy_rejects_empty_invalid_or_nonpublic_dns_results(
    addresses: tuple[str, ...], code: str
) -> None:
    async def resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return addresses

    policy = NavigationPolicy({"shop.example"}, resolver=resolver)
    assert_policy_error(policy, "https://shop.example/products", code)


def test_policy_validates_literal_public_and_private_addresses() -> None:
    public = NavigationPolicy({"8.8.8.8"})
    private = NavigationPolicy({"127.0.0.1"})

    assert run(public.validate("https://8.8.8.8/products")).addresses == ("8.8.8.8",)
    assert_policy_error(private, "https://127.0.0.1/products", "private_network_blocked")


def test_default_resolver_deduplicates_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        getaddrinfo = AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            ]
        )
        monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
        return await resolve_addresses("shop.example", 443)

    assert run(scenario()) == ("8.8.8.8",)


def test_default_resolver_sanitizes_dns_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        getaddrinfo = AsyncMock(side_effect=OSError("sensitive resolver detail"))
        monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
        with pytest.raises(NavigationPolicyError) as captured:
            await resolve_addresses("shop.example", 443)
        assert captured.value.code == "dns_resolution_failed"
        assert "sensitive" not in str(captured.value)

    run(scenario())


def test_policy_propagates_sanitized_resolver_failure() -> None:
    async def resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        raise NavigationPolicyError("dns_resolution_failed", "Sanitized resolver failure.")

    policy = NavigationPolicy({"shop.example"}, resolver=resolver)
    assert_policy_error(policy, "https://shop.example/products", "dns_resolution_failed")
