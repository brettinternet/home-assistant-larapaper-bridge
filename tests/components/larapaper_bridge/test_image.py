"""Focused tests for image URL resolution."""

from __future__ import annotations

import asyncio
import socket

import pytest

from custom_components.larapaper_bridge.image import (
    ImageNetworkPolicy,
    ImageSSRFError,
    ImageTransportError,
    ImageURLResolutionError,
    PNG_MAGIC,
    PolicyResolver,
    _REQUEST_ORIGIN,
    async_fetch_image,
    create_image_connector,
    image_origin,
    resolve_image_url,
)


BASE = "https://Larapaper.example/bridge///"
PNG_BYTES = PNG_MAGIC + b"valid"
BMP_BYTES = b"BM" + b"valid"


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = PNG_BYTES,
        read_error: BaseException | None = None,
        stream_chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._read_error = read_error
        self.read_calls = 0
        self.content = FakeContent(stream_chunks) if stream_chunks is not None else None

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        self.read_calls += 1
        if self._read_error is not None:
            raise self._read_error
        return self._body


class FakeImageSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.asyncio
async def test_transport_follows_supported_redirects_without_forwarding_headers(
    status: int,
) -> None:
    session = FakeImageSession(
        [
            FakeResponse(status, headers={"Location": "/next"}),
            FakeResponse(200, headers={"Content-Type": "image/png"}, body=PNG_BYTES),
        ]
    )

    result = await async_fetch_image(session, "https://images.example/start")

    assert result.url == "https://images.example/next"
    assert result.body == PNG_BYTES
    assert [call[0] for call in session.calls] == [
        "https://images.example/start",
        "https://images.example/next",
    ]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert all(call[1]["headers"] == {} for call in session.calls)


@pytest.mark.asyncio
async def test_transport_resolves_relative_redirects_and_allows_three_hops() -> None:
    session = FakeImageSession(
        [
            FakeResponse(301, headers={"Location": "../two"}),
            FakeResponse(302, headers={"Location": "//cdn.example/three"}),
            FakeResponse(307, headers={"Location": "four?filename=screen.bmp"}),
            FakeResponse(200, body=PNG_BYTES),
        ]
    )

    result = await async_fetch_image(session, "https://images.example/path/one")

    assert result.url == "https://cdn.example/four?filename=screen.bmp"
    assert len(session.calls) == 4


@pytest.mark.parametrize(
    "location",
    [None, "", "ftp://cdn.example/image", "https://user:secret@cdn.example/image", "/image#part"],
)
@pytest.mark.asyncio
async def test_transport_rejects_invalid_redirect_locations(location: str | None) -> None:
    session = FakeImageSession([FakeResponse(302, headers={} if location is None else {"Location": location})])

    with pytest.raises(ImageTransportError):
        await async_fetch_image(session, "https://images.example/start")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_transport_rejects_https_downgrade_and_unrecognized_redirects() -> None:
    downgrade = FakeImageSession(
        [FakeResponse(302, headers={"Location": "http://images.example/next"})]
    )
    with pytest.raises(ImageTransportError):
        await async_fetch_image(downgrade, "https://images.example/start")

    terminal = FakeImageSession(
        [FakeResponse(300, headers={"Location": "/should-not-follow"})]
    )
    with pytest.raises(ImageTransportError):
        await async_fetch_image(terminal, "https://images.example/start")

    assert len(terminal.calls) == 1


@pytest.mark.asyncio
async def test_transport_rejects_fourth_redirect() -> None:
    session = FakeImageSession(
        [
            FakeResponse(301, headers={"Location": "/one"}),
            FakeResponse(302, headers={"Location": "/two"}),
            FakeResponse(303, headers={"Location": "/three"}),
            FakeResponse(308, headers={"Location": "/four"}),
        ]
    )

    with pytest.raises(ImageTransportError, match="limit"):
        await async_fetch_image(session, "https://images.example/start")

    assert len(session.calls) == 4


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, PNG_BYTES),
        ({"Content-Type": "application/octet-stream"}, BMP_BYTES),
        ({"Content-Type": "IMAGE/PNG; charset=utf-8"}, PNG_BYTES),
        ({"Content-Type": 'image/bmp; name="screen.bmp"'}, BMP_BYTES),
    ],
)
@pytest.mark.asyncio
async def test_transport_accepts_supported_magic_and_parameterized_types(
    headers: dict[str, str], body: bytes
) -> None:
    response = FakeResponse(200, headers=headers, body=body)

    result = await async_fetch_image(FakeImageSession([response]), "https://images.example/image")

    assert result.body == body


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, b"not-an-image"),
        ({"Content-Type": "application/octet-stream"}, b"not-an-image"),
        ({"Content-Type": "image/png"}, BMP_BYTES),
        ({"Content-Type": "image/bmp"}, PNG_BYTES),
        ({"Content-Type": "text/html"}, PNG_BYTES),
        ({"Content-Type": "image/png;"}, PNG_BYTES),
        ({"Content-Type": "image/png; charset"}, PNG_BYTES),
        ({"Content-Type": 'image/png; charset="unterminated'}, PNG_BYTES),
    ],
)
@pytest.mark.asyncio
async def test_transport_rejects_unsupported_or_malformed_content_types(
    headers: dict[str, str], body: bytes
) -> None:
    with pytest.raises(ImageTransportError):
        await async_fetch_image(
            FakeImageSession([FakeResponse(200, headers=headers, body=body)]),
            "https://images.example/image",
        )


@pytest.mark.asyncio
async def test_transport_rejects_content_length_before_reading_body() -> None:
    response = FakeResponse(
        200,
        headers={"Content-Length": "11"},
        body=PNG_BYTES,
    )

    with pytest.raises(ImageTransportError, match="byte limit"):
        await async_fetch_image(
            FakeImageSession([response]),
            "https://images.example/image",
            max_image_bytes=10,
        )

    assert response.read_calls == 0


@pytest.mark.asyncio
async def test_transport_rejects_streamed_body_as_soon_as_limit_is_exceeded() -> None:
    response = FakeResponse(
        200,
        stream_chunks=[PNG_MAGIC, b"1234", b"5678"],
    )

    with pytest.raises(ImageTransportError, match="byte limit"):
        await async_fetch_image(
            FakeImageSession([response]),
            "https://images.example/image",
            max_image_bytes=len(PNG_MAGIC) + 5,
        )


@pytest.mark.asyncio
async def test_transport_accepts_body_at_inclusive_limit() -> None:
    result = await async_fetch_image(
        FakeImageSession(
            [
                FakeResponse(
                    200,
                    stream_chunks=[PNG_MAGIC, b"1234"],
                )
            ]
        ),
        "https://images.example/image",
        max_image_bytes=len(PNG_MAGIC) + 4,
    )

    assert result.body == PNG_MAGIC + b"1234"


@pytest.mark.asyncio
async def test_transport_uses_one_timeout_for_body_and_redirect_chain() -> None:
    async def slow_body() -> bytes:
        await asyncio.sleep(0.05)
        return b"late"

    response = FakeResponse(200)
    response.read = slow_body  # type: ignore[method-assign]
    session = FakeImageSession([response])

    with pytest.raises(ImageTransportError):
        await async_fetch_image(session, "https://images.example/start", timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_transport_preserves_cancellation() -> None:
    session = FakeImageSession([FakeResponse(200, read_error=asyncio.CancelledError())])

    with pytest.raises(asyncio.CancelledError):
        await async_fetch_image(session, "https://images.example/start")
