"""Focused tests for image URL resolution."""

from __future__ import annotations

import socket
import pytest

from custom_components.larapaper_bridge.image import (
    ImageNetworkPolicy,
    ImageSSRFError,
    ImageURLResolutionError,
    PolicyResolver,
    _REQUEST_ORIGIN,
    create_image_connector,
    image_origin,
    resolve_image_url,
)


BASE = "https://Larapaper.example/bridge///"


@pytest.mark.parametrize(
    ("image_url", "expected"),
    [
        ("/image.png", "https://larapaper.example/bridge/image.png"),
        ("image.png?filename=screen.bmp", "https://larapaper.example/bridge/image.png?filename=screen.bmp"),
        ("/nested/screen.bmp?special_function=raw", "https://larapaper.example/bridge/nested/screen.bmp?special_function=raw"),
    ],
)
def test_resolve_relative_paths_preserves_larapaper_prefix(
    image_url: str, expected: str
) -> None:
    assert resolve_image_url(image_url, larapaper_base_url=BASE) == expected


def test_absolute_source_preserves_path_and_query() -> None:
    assert (
        resolve_image_url(
            "https://cdn.example/assets/screen.bmp?filename=screen.bmp",
            larapaper_base_url=BASE,
        )
        == "https://cdn.example/assets/screen.bmp?filename=screen.bmp"
    )


def test_image_base_override_replaces_only_origin() -> None:
    assert (
        resolve_image_url(
            "/assets/screen.bmp?filename=screen.bmp",
            larapaper_base_url=BASE,
            image_base_url="HTTPS://Images.example:443/",
        )
        == "https://images.example/bridge/assets/screen.bmp?filename=screen.bmp"
    )


@pytest.mark.parametrize(
    "image_url",
    [
        "",
        "   ",
        "//",
        "//cdn.example/screen.png",
        "ftp://cdn.example/screen.png",
        "https://user:secret@cdn.example/screen.png",
        "https://cdn.example/screen.png#",
        "https://cdn.example/screen.png#fragment",
        "https://[invalid/screen.png",
        "https://cdn.example:invalid/screen.png",
    ],
)
def test_rejects_invalid_source_urls(image_url: str) -> None:
    with pytest.raises(ImageURLResolutionError):
        resolve_image_url(image_url, larapaper_base_url=BASE)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret.example/bridge/",
        "https://larapaper.example/bridge/?",
        "https://larapaper.example/bridge/?query=yes",
        "https://larapaper.example/bridge/#",
        "https://larapaper.example/bridge/#fragment",
        "ftp://larapaper.example/bridge/",
        "https://[invalid/bridge/",
    ],
)
def test_rejects_invalid_larapaper_base(base_url: str) -> None:
    with pytest.raises(ImageURLResolutionError):
        resolve_image_url("/screen.png", larapaper_base_url=base_url)


@pytest.mark.parametrize(
    "image_base_url",
    [
        "https://user:secret@images.example/",
        "https://images.example/assets/",
        "https://images.example/?",
        "https://images.example/?query=yes",
        "https://images.example/#",
        "ftp://images.example/",
    ],
)
def test_rejects_invalid_image_base_override(image_base_url: str) -> None:
    with pytest.raises(ImageURLResolutionError):
        resolve_image_url(
            "/screen.png",
            larapaper_base_url=BASE,
            image_base_url=image_base_url,
        )


class FakeResolver:
    """Return representative aiohttp ResolveResult dictionaries."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.closed = False

    async def resolve(
        self, host: str, port: int, family: socket.AddressFamily
    ) -> list[dict[str, object]]:
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self) -> None:
        self.closed = True


def _resolver(
    addresses: list[str],
    *,
    request_url: str | None = None,
) -> tuple[PolicyResolver, FakeResolver]:
    raw = FakeResolver(addresses)
    policy = ImageNetworkPolicy.from_urls("https://private.example/bridge/")
    return (
        PolicyResolver(
            raw,
            allowed_private_origins=policy.allowed_private_origins,
            request_origin=image_origin(request_url) if request_url else None,
        ),
        raw,
    )


@pytest.mark.asyncio
async def test_connection_policy_allows_exact_configured_private_https_origin() -> None:
    resolver, _raw = _resolver(
        ["192.168.1.20"],
        request_url="https://private.example/bridge/image.png",
    )
    results = await resolver.resolve("private.example", 443, socket.AF_UNSPEC)
    assert results[0]["host"] == "192.168.1.20"


@pytest.mark.asyncio
async def test_connection_policy_rejects_same_private_authority_on_http() -> None:
    resolver, _raw = _resolver(
        ["192.168.1.20"],
        request_url="http://private.example/bridge/image.png",
    )
    with pytest.raises(ImageSSRFError):
        await resolver.resolve("private.example", 80, socket.AF_UNSPEC)


@pytest.mark.asyncio
async def test_connection_policy_accepts_only_global_unicast_addresses() -> None:
    resolver, _raw = _resolver(
        ["93.184.216.34"],
        request_url="https://unconfigured.example/image.png",
    )
    results = await resolver.resolve("unconfigured.example", 443, socket.AF_UNSPEC)
    assert results[0]["host"] == "93.184.216.34"

    private_resolver, _raw = _resolver(
        ["127.0.0.1"],
        request_url="https://unconfigured.example/image.png",
    )
    with pytest.raises(ImageSSRFError):
        await private_resolver.resolve("unconfigured.example", 443, socket.AF_UNSPEC)


@pytest.mark.asyncio
async def test_connection_policy_rejects_mixed_dns_answers() -> None:
    resolver, _raw = _resolver(
        ["93.184.216.34", "192.168.1.20"],
        request_url="https://private.example/bridge/image.png",
    )
    with pytest.raises(ImageSSRFError, match="mixed"):
        await resolver.resolve("private.example", 443, socket.AF_UNSPEC)


@pytest.mark.asyncio
async def test_connection_policy_rechecks_dns_rebinding_on_each_resolution() -> None:
    resolver, raw = _resolver(
        ["93.184.216.34"],
        request_url="https://unconfigured.example/image.png",
    )
    await resolver.resolve("unconfigured.example", 443, socket.AF_UNSPEC)
    raw.addresses[:] = ["10.0.0.8"]
    with pytest.raises(ImageSSRFError):
        await resolver.resolve("unconfigured.example", 443, socket.AF_UNSPEC)


@pytest.mark.asyncio
async def test_connector_disables_dns_cache_and_closes_wrapped_resolver() -> None:
    raw = FakeResolver(["93.184.216.34"])
    policy = ImageNetworkPolicy.from_urls("https://private.example/bridge/")
    connector = create_image_connector(policy, resolver=raw)
    assert connector.use_dns_cache is False
    await connector.close()
    assert raw.closed is True


@pytest.mark.asyncio
async def test_connector_validates_literal_ip_resolution_with_request_scheme() -> None:
    raw = FakeResolver([])
    policy = ImageNetworkPolicy.from_urls("https://127.0.0.1/bridge/")
    connector = create_image_connector(policy, resolver=raw)
    token = _REQUEST_ORIGIN.set(image_origin("https://127.0.0.1/bridge/image.png"))
    try:
        results = await connector._resolve_host("127.0.0.1", 443)
    finally:
        _REQUEST_ORIGIN.reset(token)
        await connector.close()
    assert results[0]["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_connector_rejects_literal_ip_for_wrong_request_scheme() -> None:
    raw = FakeResolver([])
    policy = ImageNetworkPolicy.from_urls("https://127.0.0.1/bridge/")
    connector = create_image_connector(policy, resolver=raw)
    token = _REQUEST_ORIGIN.set(image_origin("http://127.0.0.1/bridge/image.png"))
    try:
        with pytest.raises(ImageSSRFError):
            await connector._resolve_host("127.0.0.1", 80)
    finally:
        _REQUEST_ORIGIN.reset(token)
        await connector.close()
