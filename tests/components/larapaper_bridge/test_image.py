"""Focused tests for image URL resolution."""

from __future__ import annotations

from aiohttp import DummyCookieJar

import asyncio
import socket
import random
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from custom_components.larapaper_bridge.image import (
    BMP_MAGIC,
    MAX_DECODED_DIMENSION,
    MAX_DECODED_PIXELS,
    BoundedImageOperation,
    ImageConversionError,
    ImageDimensions,
    ImageNetworkPolicy,
    ImageResources,
    ImageResponse,
    ImageSSRFError,
    ImageTransportError,
    ImageURLResolutionError,
    ImageValidationError,
    PNG_MAGIC,
    PolicyResolver,
    _REQUEST_ORIGIN,
    async_fetch_image,
    async_get_image_resources,
    convert_image_to_png,
    create_image_connector,
    image_origin,
    resolve_image_url,
    validate_image_dimensions,
)
from custom_components.larapaper_bridge.scheduler import OperationToken



BASE = "https://Larapaper.example/bridge///"
PNG_BYTES = PNG_MAGIC + b"valid"
BMP_BYTES = b"BM" + b"valid"


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk
def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _png_bytes(width: int, height: int, *, pixels: bool = True) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    if pixels:
        raw = b"".join(
            b"\x00" + (b"\x20\x80\xe0\xff" * width) for _ in range(height)
        )
        idat = zlib.compress(raw)
    else:
        idat = b""
    return PNG_MAGIC + _png_chunk(b"IHDR", ihdr) + _png_chunk(
        b"IDAT", idat
    ) + _png_chunk(b"IEND", b"")


def _bmp_bytes(
    width: int, height: int, *, pixels: bool = True, top_down: bool = False
) -> bytes:
    row_stride = (width * 3 + 3) & ~3
    pixel_data = (
        bytes((index * 37 + 11) % 256 for index in range(row_stride * height))
        if pixels
        else b""
    )
    signed_height = -height if top_down else height
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        signed_height,
        1,
        24,
        0,
        len(pixel_data),
        0,
        0,
        0,
        0,
    )
    file_size = 14 + len(dib) + len(pixel_data)
    header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, 54)
    return header + dib + pixel_data


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
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("width", "height", "valid"),
    [
        (1, 1, True),
        (MAX_DECODED_DIMENSION, MAX_DECODED_PIXELS // MAX_DECODED_DIMENSION, True),
        (MAX_DECODED_DIMENSION, MAX_DECODED_PIXELS // MAX_DECODED_DIMENSION + 1, False),
        (MAX_DECODED_DIMENSION + 1, 1, False),
        (0, 1, False),
    ],
)
def test_png_dimensions_are_structural_and_bounded(
    width: int, height: int, valid: bool
) -> None:
    body = _png_bytes(width, height, pixels=False)
    if valid:
        assert validate_image_dimensions(body) == ImageDimensions(
            "png", width, height
        )
    else:
        with pytest.raises(ImageValidationError):
            validate_image_dimensions(body)


@pytest.mark.parametrize(
    ("width", "height", "valid"),
    [
        (1, 1, True),
        (MAX_DECODED_DIMENSION, MAX_DECODED_PIXELS // MAX_DECODED_DIMENSION, True),
        (MAX_DECODED_DIMENSION, MAX_DECODED_PIXELS // MAX_DECODED_DIMENSION + 1, False),
        (MAX_DECODED_DIMENSION + 1, 1, False),
        (0, 1, False),
    ],
)
def test_bmp_dimensions_handle_signed_height_and_bounds(
    width: int, height: int, valid: bool
) -> None:
    body = _bmp_bytes(width, height, pixels=False)
    if valid:
        assert validate_image_dimensions(body) == ImageDimensions(
            "bmp", width, height
        )
        top_down = _bmp_bytes(width, height, pixels=False, top_down=True)
        assert validate_image_dimensions(top_down) == ImageDimensions(
            "bmp", width, height
        )
    else:
        with pytest.raises(ImageValidationError):
            validate_image_dimensions(body)


@pytest.mark.parametrize(
    "body",
    [
        PNG_MAGIC,
        _png_bytes(1, 1, pixels=False)[:-1],
        _png_bytes(1, 1, pixels=False)[:29]
        + bytes([_png_bytes(1, 1, pixels=False)[29] ^ 1])
        + _png_bytes(1, 1, pixels=False)[30:],
        _bmp_bytes(1, 1, pixels=False)[:30],
    ],
)
def test_image_dimension_preflight_rejects_truncated_or_corrupt_headers(
    body: bytes,
) -> None:
    with pytest.raises(ImageValidationError):
        validate_image_dimensions(body)


def test_valid_png_is_passed_through_without_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.larapaper_bridge import image as image_module

    monkeypatch.setattr(
        image_module.Image,
        "open",
        lambda *_args, **_kwargs: pytest.fail("PNG must not use Pillow"),
    )
    body = _png_bytes(2, 2)

    assert convert_image_to_png(body, max_image_bytes=100_000) is body


def test_bmp_is_decoded_and_converted_to_png() -> None:
    body = _bmp_bytes(3, 2)

    converted = convert_image_to_png(body, max_image_bytes=100_000)

    assert isinstance(converted, bytes)
    assert converted.startswith(PNG_MAGIC)
    assert validate_image_dimensions(converted) == ImageDimensions("png", 3, 2)


def test_truncated_bmp_is_rejected_after_header_preflight() -> None:
    with pytest.raises(ImageValidationError):
        convert_image_to_png(
            _bmp_bytes(3, 2, pixels=False),
            max_image_bytes=1_000,
        )


@pytest.mark.parametrize("bomb", [False, True])
def test_pillow_bomb_warning_and_error_are_hard_validation_failures(
    monkeypatch: pytest.MonkeyPatch, bomb: bool
) -> None:
    from custom_components.larapaper_bridge import image as image_module

    if bomb:
        def open_bomb(*_args: object, **_kwargs: object) -> object:
            raise image_module.Image.DecompressionBombError
    else:
        def open_bomb(*_args: object, **_kwargs: object) -> object:
            image_module.warnings.warn(
                "bomb",
                image_module.Image.DecompressionBombWarning,
            )
            raise AssertionError("warning should become an exception")

    monkeypatch.setattr(image_module.Image, "open", open_bomb)
    with pytest.raises(ImageValidationError):
        convert_image_to_png(_bmp_bytes(1, 1), max_image_bytes=1_000)


def test_converted_output_limit_is_checked_before_publication() -> None:
    body = bytearray(_bmp_bytes(64, 64))
    body[54:] = random.Random(17).randbytes(len(body) - 54)
    body = bytes(body)
    converted = convert_image_to_png(body, max_image_bytes=1_000_000)
    assert len(converted) > len(body)

    with pytest.raises(ImageConversionError):
        convert_image_to_png(
            body,
            max_image_bytes=max(len(body), len(converted) - 1),
        )


def test_conversion_does_not_write_to_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("image conversion must not open filesystem paths")

    monkeypatch.setattr("builtins.open", fail_open)
    converted = convert_image_to_png(_bmp_bytes(2, 2), max_image_bytes=100_000)
    assert converted.startswith(PNG_MAGIC)


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


@pytest.mark.parametrize(
    "image_url",
    [
        "/../secret.png",
        "../secret.png",
        "/%2e%2e/secret.png",
        "%2E%2E/secret.png",
        "/%2e%2f%2e%2e/secret.png",
    ],
)
def test_rejects_relative_dot_segments(image_url: str) -> None:
    with pytest.raises(ImageURLResolutionError):
        resolve_image_url(image_url, larapaper_base_url=BASE)


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
        "https://cdn.example/screen.png?",
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
async def test_image_operation_classifies_content_type_failures_as_validation() -> None:
    class FakeBus:
        def async_listen_once(self, _event: str, _callback: object) -> None:
            return None

    class FakeHass:
        bus = FakeBus()

    executor = ThreadPoolExecutor(max_workers=1)
    resources = ImageResources(
        FakeHass(),  # type: ignore[arg-type]
        session=FakeImageSession(
            [FakeResponse(200, headers={"Content-Type": "text/html"}, body=PNG_BYTES)]
        ),
        executor=executor,
        policy=ImageNetworkPolicy.from_urls(BASE),
    )
    operation = BoundedImageOperation(
        resources, larapaper_base_url=BASE, max_image_bytes=100_000
    )

    outcome = await operation.async_process(
        "/image.png", OperationToken(0, 1)
    )

    assert outcome.error_code == "validation"
    await resources._async_stop(None)


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


@pytest.mark.asyncio
async def test_image_resources_reuse_and_final_stop_cleanup() -> None:
    class FakeBus:
        def __init__(self) -> None:
            self.listeners: list[tuple[str, object]] = []

        def async_listen_once(self, event: str, callback: object) -> None:
            self.listeners.append((event, callback))

    class FakeHass:
        def __init__(self) -> None:
            self.data: dict[str, object] = {}
            self.bus = FakeBus()

    class FakeExecutor:
        def __init__(self) -> None:
            self.shutdown_calls: list[tuple[bool, bool]] = []

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    hass = FakeHass()
    session = FakeImageSession([])
    executor = FakeExecutor()
    resources = await async_get_image_resources(
        hass,
        larapaper_base_url=BASE,
        session=session,
        executor=executor,  # type: ignore[arg-type]
    )
    same_policy_resources = await async_get_image_resources(
        hass,
        # The normalized authority is the same despite a different path/case.
        larapaper_base_url="https://larapaper.example/other/",
        session=FakeImageSession([]),
        executor=FakeExecutor(),  # type: ignore[arg-type]
    )
    assert same_policy_resources is resources
    with pytest.raises(RuntimeError, match="policy cannot change"):
        await async_get_image_resources(
            hass,
            larapaper_base_url="https://other.example/",
            session=FakeImageSession([]),
            executor=FakeExecutor(),  # type: ignore[arg-type]
        )
    assert len(hass.bus.listeners) == 1

    hass_with_image_base = FakeHass()
    image_base_session = FakeImageSession([])
    image_base_executor = FakeExecutor()
    image_base_resources = await async_get_image_resources(
        hass_with_image_base,
        larapaper_base_url=BASE,
        image_base_url="https://images.example/",
        session=image_base_session,
        executor=image_base_executor,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="policy cannot change"):
        await async_get_image_resources(
            hass_with_image_base,
            larapaper_base_url=BASE,
            image_base_url=None,
            session=FakeImageSession([]),
            executor=FakeExecutor(),  # type: ignore[arg-type]
        )
    assert image_base_resources.closed is False
    assert image_base_session.closed is False
    assert image_base_executor.shutdown_calls == []
    await hass_with_image_base.bus.listeners[0][1](None)  # type: ignore[misc]

    callback = hass.bus.listeners[0][1]
    await callback(None)  # type: ignore[misc]
    assert session.closed is True
    assert executor.shutdown_calls == [(False, True)]


@pytest.mark.asyncio
async def test_conversion_completion_skips_release_when_loop_is_closed() -> None:
    class FakeBus:
        def async_listen_once(self, _event: str, _callback: object) -> None:
            return None

    class FakeHass:
        bus = FakeBus()

    class ClosedLoop:
        def call_soon_threadsafe(self, *_args: object) -> None:
            raise RuntimeError("event loop is closed")

    resources = ImageResources(
        FakeHass(),  # type: ignore[arg-type]
        session=FakeImageSession([]),
        executor=ThreadPoolExecutor(max_workers=1),
        policy=ImageNetworkPolicy.from_urls(BASE),
    )
    future = object()
    resources._conversion_future = future  # type: ignore[assignment]
    resources._loop = ClosedLoop()  # type: ignore[assignment]

    resources._conversion_done(future)  # type: ignore[arg-type]

    assert resources._conversion_future is future
    await resources._async_stop(None)


@pytest.mark.asyncio
async def test_image_operation_holds_admission_until_abandoned_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.larapaper_bridge import image as image_module

    started = threading.Event()
    release = threading.Event()

    def blocking_conversion(body: bytes, *, max_image_bytes: int) -> bytes:
        del max_image_bytes
        started.set()
        assert release.wait(2)
        return body

    monkeypatch.setattr(
        image_module, "convert_image_to_png", blocking_conversion
    )
    session = FakeImageSession(
        [
            FakeResponse(200, body=_png_bytes(1, 1)),
            FakeResponse(200, body=_png_bytes(1, 1)),
        ]
    )
    executor = ThreadPoolExecutor(max_workers=1)

    class FakeBus:
        def async_listen_once(self, _event: str, _callback: object) -> None:
            return None

    class FakeHass:
        bus = FakeBus()

    resources = ImageResources(
        FakeHass(),
        session=session,
        executor=executor,
        policy=ImageNetworkPolicy.from_urls(BASE),
    )
    operation = BoundedImageOperation(
        resources, larapaper_base_url=BASE, max_image_bytes=100_000
    )
    first_token = OperationToken(0, 1)
    first_task = asyncio.create_task(
        operation.async_process("/first.png", first_token)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    second = await operation.async_process(
        "/second.png", OperationToken(0, 2)
    )
    assert second.error_code == "conversion"
    assert len(session.calls) == 2

    operation.abandon(first_token)
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert resources._conversion_future is not None

    release.set()
    for _ in range(100):
        if resources._conversion_future is None:
            break
        await asyncio.sleep(0.001)
    assert resources._conversion_future is None
    await resources._async_stop(None)

@pytest.mark.asyncio
async def test_image_operation_keeps_all_prior_generations_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.larapaper_bridge import image as image_module

    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    conversions = 0

    async def delayed_fetch(*_args: object, **_kwargs: object) -> ImageResponse:
        fetch_started.set()
        await release_fetch.wait()
        return ImageResponse(
            url="https://larapaper.example/bridge/image.png",
            status=200,
            headers={"Content-Type": "image/png"},
            body=PNG_BYTES,
        )

    def conversion(_body: bytes, *, max_image_bytes: int) -> bytes:
        nonlocal conversions
        del max_image_bytes
        conversions += 1
        return PNG_BYTES

    monkeypatch.setattr(image_module, "async_fetch_image", delayed_fetch)
    monkeypatch.setattr(image_module, "convert_image_to_png", conversion)

    class FakeBus:
        def async_listen_once(self, _event: str, _callback: object) -> None:
            return None

    class FakeHass:
        bus = FakeBus()

    executor = ThreadPoolExecutor(max_workers=1)
    resources = ImageResources(
        FakeHass(),
        session=FakeImageSession([]),
        executor=executor,
        policy=ImageNetworkPolicy.from_urls(BASE),
    )
    operation = BoundedImageOperation(
        resources, larapaper_base_url=BASE, max_image_bytes=100_000
    )
    first_token = OperationToken(0, 1)
    second_token = OperationToken(0, 2)
    first_task = asyncio.create_task(
        operation.async_process("/first.png", first_token)
    )

    await fetch_started.wait()
    operation.abandon(first_token)
    operation.abandon(second_token)
    release_fetch.set()

    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert conversions == 0
    assert resources._conversion_future is None
    await resources._async_stop(None)

@pytest.mark.asyncio
async def test_created_image_sessions_disable_cookie_storage(monkeypatch):
    from custom_components.larapaper_bridge import image as image_module

    class FakeBus:
        def async_listen_once(self, _event: str, _callback: object) -> None:
            return None

    class FakeHass:

        data: dict[str, object] = {}
        bus = FakeBus()

    sessions: list[dict[str, object]] = []

    def session_factory(**kwargs: object) -> FakeImageSession:
        sessions.append(kwargs)
        return FakeImageSession([])

    monkeypatch.setattr(image_module, "ClientSession", session_factory)
    monkeypatch.setattr(
        image_module, "create_image_connector", lambda _policy: object()
    )
    policy = ImageNetworkPolicy.from_urls(BASE)

    resources = await ImageResources.async_create(FakeHass(), policy)
    resources_from_factory = await async_get_image_resources(
        FakeHass(), larapaper_base_url=BASE
    )

    assert len(sessions) == 2
    assert all(isinstance(item["cookie_jar"], DummyCookieJar) for item in sessions)
    await resources._async_stop(None)
    await resources_from_factory._async_stop(None)