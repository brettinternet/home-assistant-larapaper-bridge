"""Image URL resolution and connection-time network policy."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import ipaddress
import math
import re
import socket
import struct
import threading
import warnings
import zlib
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.client_exceptions import ClientConnectorError
from aiohttp.resolver import DefaultResolver
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from PIL import Image, ImageFile
from PIL import UnidentifiedImageError

from .const import DEFAULT_MAX_IMAGE_BYTES

if TYPE_CHECKING:
    from .scheduler import ImageOutcome, OperationToken




class ImageURLResolutionError(ValueError):
    """Raised when an image URL cannot be safely resolved."""


class ImageSSRFError(OSError):
    """Raised when DNS results do not satisfy the image-origin policy."""


class ImageTransportError(OSError):
    """Raised when an image request cannot complete safely."""


class ImageValidationError(ImageTransportError):
    """Raised when image bytes fail structural or decoded-image validation."""


class ImageConversionError(ImageTransportError):
    """Raised when a validated BMP cannot be converted to bounded PNG bytes."""

@dataclass(frozen=True, slots=True)
class ImageResponse:
    """The final response returned by the image transport."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ImageDimensions:
    """Validated dimensions for one supported encoded image."""

    format: Literal["png", "bmp"]
    width: int
    height: int


MAX_DECODED_DIMENSION = 8192
MAX_DECODED_PIXELS = 16_777_216
_PNG_IHDR_LENGTH = 13
_BMP_FILE_HEADER_BYTES = 14
_PILLOW_LOCK = threading.Lock()



PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
BMP_MAGIC = b"BM"
MAX_BODY_CHUNK_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
IMAGE_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_IMAGE_REDIRECTS = 3


@dataclass(frozen=True, slots=True)
class ImageOrigin:
    """The normalized authority and protocol of one approved image origin."""

    scheme: str
    hostname: str
    port: int


_REQUEST_ORIGIN: ContextVar[ImageOrigin | None] = ContextVar(
    "larapaper_image_request_origin", default=None
)

def _parse_http_url(value: object, *, allow_relative: bool = False) -> SplitResult:
    """Parse an HTTP(S) URL without accepting credentials or fragments."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ImageURLResolutionError
    if "#" in value or (allow_relative and value.startswith("//")):
        raise ImageURLResolutionError
    try:
        parsed = urlsplit(value)
        if not allow_relative and parsed.scheme.lower() not in {"http", "https"}:
            raise ImageURLResolutionError
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            raise ImageURLResolutionError
        if not allow_relative and not parsed.hostname:
            raise ImageURLResolutionError
        if parsed.netloc and not parsed.hostname:
            raise ImageURLResolutionError
        if parsed.username is not None or parsed.password is not None:
            raise ImageURLResolutionError
        if parsed.fragment:
            raise ImageURLResolutionError
        parsed.port
    except (TypeError, ValueError) as error:
        raise ImageURLResolutionError from error
    return parsed


def _normalized_authority(parsed: SplitResult) -> str:
    """Return a lowercase host with only a non-default port."""
    hostname = parsed.hostname
    if not hostname:
        raise ImageURLResolutionError
    hostname = hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    return hostname if port in (None, default_port) else f"{hostname}:{port}"


def _normalized_absolute_url(parsed: SplitResult, *, scheme: str | None = None) -> str:
    """Canonicalize authority while preserving source path and query."""
    selected_scheme = (scheme or parsed.scheme).lower()
    if selected_scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageURLResolutionError
    authority = _normalized_authority(
        SplitResult(selected_scheme, parsed.netloc, parsed.path, parsed.query, "")
    )
    return urlunsplit((selected_scheme, authority, parsed.path, parsed.query, ""))


def _reject_dot_segments(path: str) -> None:
    """Reject literal or percent-encoded path traversal segments."""
    if any(segment in {".", ".."} for segment in unquote(path).split("/")):
        raise ImageURLResolutionError


def resolve_image_url(
    image_url: str,
    *,
    larapaper_base_url: str,
    image_base_url: str | None = None,
) -> str:
    """Resolve one Larapaper image URL while preserving the API path prefix.

    Relative and root-relative image paths remain below the configured
    Larapaper pathname prefix. An optional image base replaces only the
    resolved scheme, hostname, and effective port.
    """
    base = _parse_http_url(larapaper_base_url)
    if "?" in larapaper_base_url or base.query or base.fragment:
        raise ImageURLResolutionError
    base_path = base.path.rstrip("/") + "/"

    source = _parse_http_url(image_url, allow_relative=True)
    if image_url.endswith("?"):
        raise ImageURLResolutionError
    if source.scheme or source.netloc:
        resolved = _normalized_absolute_url(source, scheme=source.scheme or base.scheme)
    else:
        path = source.path[1:] if source.path.startswith("/") else source.path
        _reject_dot_segments(source.path)
        resolved = urlunsplit(
            (
                base.scheme.lower(),
                _normalized_authority(
                    SplitResult(base.scheme.lower(), base.netloc, base.path, "", "")
                ),
                base_path + path,
                source.query,
                "",
            )
        )

    resolved_parts = _parse_http_url(resolved)
    if image_base_url is not None and image_base_url.strip():
        override = _parse_http_url(image_base_url)
        if "?" in image_base_url or override.path not in ("", "/") or override.query or override.fragment:
            raise ImageURLResolutionError
        resolved = urlunsplit(
            (
                override.scheme.lower(),
                _normalized_authority(override),
                resolved_parts.path,
                resolved_parts.query,
                "",
            )
        )
    return resolved


def _redirect_target(current_url: str, location: object) -> str:
    """Resolve and validate one redirect destination."""
    if not isinstance(location, str) or not location:
        raise ImageTransportError("image redirect had no valid location")
    target = urljoin(current_url, location)
    try:
        parsed = _parse_http_url(target)
        current_scheme = urlsplit(current_url).scheme.lower()
        if current_scheme == "https" and parsed.scheme.lower() != "https":
            raise ImageTransportError("image redirect downgraded HTTPS")
        return _normalized_absolute_url(parsed)
    except ImageTransportError:
        raise
    except ImageURLResolutionError:
        raise ImageTransportError("image redirect had an invalid location") from None


@contextlib.asynccontextmanager
async def _image_request(
    session: Any,
    url: str,
    *,
    timeout: ClientTimeout,
) -> AsyncIterator[Any]:
    """Support aiohttp requests and deterministic async test fakes."""
    request = session.get(
        url,
        headers={},
        allow_redirects=False,
        timeout=timeout,
    )
    if hasattr(request, "__aenter__"):
        async with request as response:
            yield response
        return
    if inspect.isawaitable(request):
        request = await request
    yield request



def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read one response header without depending on mapping case."""
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _valid_quoted_parameter(value: str) -> bool:
    """Return whether a content-type quoted parameter is well formed."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return False
    escaped = False
    for char in value[1:-1]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            return False
    return not escaped


def _declared_media_type(headers: Mapping[str, str]) -> str | None:
    """Normalize and validate Content-Type, stripping valid parameters."""
    value = _header_value(headers, "Content-Type")
    if value is None:
        return None
    parts = value.split(";")
    media_type = parts[0].strip().lower()
    type_parts = media_type.split("/")
    if len(type_parts) != 2 or not all(_TOKEN_RE.fullmatch(part) for part in type_parts):
        raise ImageValidationError("image content type was invalid")
    for parameter in parts[1:]:
        name_value = parameter.strip().split("=", 1)
        if len(name_value) != 2 or not _TOKEN_RE.fullmatch(name_value[0].strip()):
            raise ImageValidationError("image content type was invalid")
        parameter_value = name_value[1].strip()
        if not (
            _TOKEN_RE.fullmatch(parameter_value)
            or _valid_quoted_parameter(parameter_value)
        ):
            raise ImageValidationError("image content type was invalid")
    return media_type


async def _read_limited_body(response: Any, limit: int) -> bytes:
    """Read one response body while enforcing its encoded-byte limit."""
    headers = response.headers
    content_length = _header_value(headers, "Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length.strip())
        except (AttributeError, TypeError, ValueError):
            raise ImageTransportError("image content length was invalid") from None
        if declared_length < 0 or declared_length > limit:
            raise ImageTransportError("image response exceeded the byte limit")

    content = getattr(response, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        body = bytearray()
        async for chunk in iter_chunked(MAX_BODY_CHUNK_BYTES):
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ImageTransportError("image response body was invalid")
            if len(body) + len(chunk) > limit:
                raise ImageTransportError("image response exceeded the byte limit")
            body.extend(chunk)
        return bytes(body)

    body = await response.read()
    if not isinstance(body, (bytes, bytearray, memoryview)) or len(body) > limit:
        raise ImageTransportError("image response exceeded the byte limit")
    return bytes(body)


def _validate_image_bytes(body: bytes, headers: Mapping[str, str]) -> None:
    """Enforce the declared media type and image magic-byte contract."""
    media_type = _declared_media_type(headers)
    is_png = body.startswith(PNG_MAGIC)
    is_bmp = body.startswith(BMP_MAGIC)
    if media_type is None or media_type == "application/octet-stream":
        if not (is_png or is_bmp):
            raise ImageValidationError("image bytes did not match a supported format")
        return
    if media_type == "image/png":
        if not is_png:
            raise ImageValidationError("image bytes did not match PNG")
        return
    if media_type == "image/bmp":
        if not is_bmp:
            raise ImageValidationError("image bytes did not match BMP")
        return
    raise ImageValidationError("image content type was unsupported")


def _checked_dimensions(
    image_format: Literal["png", "bmp"], width: int, height: int
) -> ImageDimensions:
    """Apply the fixed axis and decoded-pixel limits without overflow."""
    if (
        not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
        or width > MAX_DECODED_DIMENSION
        or height > MAX_DECODED_DIMENSION
        or width > MAX_DECODED_PIXELS // height
    ):
        raise ImageValidationError("image dimensions exceeded the decode limits")
    return ImageDimensions(image_format, width, height)


def _parse_png_dimensions(body: bytes) -> ImageDimensions:
    """Validate PNG chunks and return dimensions without Pillow."""
    if len(body) < len(PNG_MAGIC) or not body.startswith(PNG_MAGIC):
        raise ImageValidationError("image was not a PNG")

    offset = len(PNG_MAGIC)
    dimensions: ImageDimensions | None = None
    saw_idat = False
    while offset < len(body):
        if len(body) - offset < 12:
            raise ImageValidationError("PNG chunk was truncated")
        chunk_length = struct.unpack_from(">I", body, offset)[0]
        chunk_type = body[offset + 4 : offset + 8]
        if not all(
            65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type
        ):
            raise ImageValidationError("PNG chunk type was invalid")
        data_start = offset + 8
        data_end = data_start + chunk_length
        chunk_end = data_end + 4
        if data_end < data_start or chunk_end > len(body):
            raise ImageValidationError("PNG chunk was truncated")
        chunk_data = body[data_start:data_end]
        expected_crc = struct.unpack_from(">I", body, data_end)[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ImageValidationError("PNG chunk checksum was invalid")

        if dimensions is None:
            if chunk_type != b"IHDR" or chunk_length != _PNG_IHDR_LENGTH:
                raise ImageValidationError("PNG IHDR was invalid")
            width, height = struct.unpack_from(">II", chunk_data)
            dimensions = _checked_dimensions("png", width, height)
        elif chunk_type == b"IHDR":
            raise ImageValidationError("PNG contained multiple IHDR chunks")

        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if chunk_length != 0 or not saw_idat or chunk_end != len(body):
                raise ImageValidationError("PNG ended before a complete image")
            return dimensions
        offset = chunk_end

    raise ImageValidationError("PNG did not contain a complete IEND chunk")


def _parse_bmp_dimensions(body: bytes) -> ImageDimensions:
    """Validate the BMP header and return signed-height-safe dimensions."""
    if len(body) < _BMP_FILE_HEADER_BYTES or not body.startswith(BMP_MAGIC):
        raise ImageValidationError("image was not a BMP")
    pixel_offset = struct.unpack_from("<I", body, 10)[0]
    if len(body) < _BMP_FILE_HEADER_BYTES + 4:
        raise ImageValidationError("BMP DIB header was truncated")
    dib_size = struct.unpack_from("<I", body, _BMP_FILE_HEADER_BYTES)[0]

    if dib_size == 12:
        dib_end = _BMP_FILE_HEADER_BYTES + dib_size
        if len(body) < dib_end:
            raise ImageValidationError("BMP DIB header was truncated")
        width, height = struct.unpack_from("<HH", body, 18)
    elif dib_size >= 40:
        dib_end = _BMP_FILE_HEADER_BYTES + dib_size
        if len(body) < dib_end:
            raise ImageValidationError("BMP DIB header was truncated")
        width, signed_height = struct.unpack_from("<ii", body, 18)
        if signed_height == 0:
            raise ImageValidationError("BMP height was invalid")
        height = abs(signed_height)
    else:
        raise ImageValidationError("BMP DIB header was unsupported")

    if pixel_offset < dib_end:
        raise ImageValidationError("BMP pixel offset was invalid")
    if pixel_offset > len(body):
        raise ImageValidationError("BMP pixel data was truncated")
    return _checked_dimensions("bmp", width, height)


def validate_image_dimensions(body: bytes) -> ImageDimensions:
    """Validate supported image structure and bounded decoded dimensions."""
    if not isinstance(body, bytes):
        raise ImageValidationError("image bytes must be immutable bytes")
    if body.startswith(PNG_MAGIC):
        return _parse_png_dimensions(body)
    if body.startswith(BMP_MAGIC):
        return _parse_bmp_dimensions(body)
    raise ImageValidationError("image format was unsupported")


def _validate_max_image_bytes(max_image_bytes: int) -> None:
    """Validate the configured encoded and converted byte limit."""
    if (
        not isinstance(max_image_bytes, int)
        or isinstance(max_image_bytes, bool)
        or max_image_bytes <= 0
    ):
        raise ValueError("max_image_bytes must be positive")


def convert_image_to_png(body: bytes, *, max_image_bytes: int) -> bytes:
    """Return a bounded PNG, using Pillow only for BMP conversion."""
    _validate_max_image_bytes(max_image_bytes)
    if len(body) > max_image_bytes:
        raise ImageValidationError("image response exceeded the byte limit")
    dimensions = validate_image_dimensions(body)
    if dimensions.format == "png":
        return body

    previous_truncated_setting = ImageFile.LOAD_TRUNCATED_IMAGES
    try:
        with _PILLOW_LOCK:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                try:
                    with Image.open(io.BytesIO(body)) as decoded:
                        if decoded.format != "BMP":
                            raise ImageValidationError("image format was not BMP")
                        loaded_dimensions = _checked_dimensions(
                            "bmp", *decoded.size
                        )
                        if loaded_dimensions != dimensions:
                            raise ImageValidationError(
                                "BMP dimensions changed during decode"
                            )
                        decoded.load()
                        loaded_dimensions = _checked_dimensions(
                            "bmp", *decoded.size
                        )
                        if loaded_dimensions != dimensions:
                            raise ImageValidationError(
                                "BMP dimensions changed during decode"
                            )
                        try:
                            converted = decoded.convert("RGB")
                            output = io.BytesIO()
                            converted.save(output, format="PNG")
                        except ImageValidationError:
                            raise
                        except Exception as error:
                            raise ImageConversionError(
                                "BMP conversion failed"
                            ) from error
                        finally:
                            if "converted" in locals():
                                converted.close()
                        png_bytes = output.getvalue()
                except ImageValidationError:
                    raise
                except (
                    Image.DecompressionBombError,
                    Image.DecompressionBombWarning,
                    UnidentifiedImageError,
                    EOFError,
                    OSError,
                    SyntaxError,
                    ValueError,
                ) as error:
                    raise ImageValidationError("image decode failed") from error
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated_setting

    if len(png_bytes) > max_image_bytes:
        raise ImageConversionError("converted PNG exceeded the byte limit")
    return bytes(png_bytes)

async def async_fetch_image(
    session: Any,
    image_url: str,
    *,
    larapaper_base_url: str | None = None,
    image_base_url: str | None = None,
    timeout_seconds: float = IMAGE_REQUEST_TIMEOUT_SECONDS,
    max_redirects: int = MAX_IMAGE_REDIRECTS,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> ImageResponse:
    """Fetch one image response with bounded, policy-aware redirects.

    The session must use ``create_image_connector`` so DNS policy is enforced
    for the initial URL and every redirect destination.
    """
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be positive")
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not isinstance(max_redirects, int) or isinstance(max_redirects, bool) or max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")
    if (
        not isinstance(max_image_bytes, int)
        or isinstance(max_image_bytes, bool)
        or max_image_bytes <= 0
    ):
        raise ValueError("max_image_bytes must be positive")

    if larapaper_base_url is not None:
        current_url = resolve_image_url(
            image_url,
            larapaper_base_url=larapaper_base_url,
            image_base_url=image_base_url,
        )
    else:
        try:
            current_url = _normalized_absolute_url(_parse_http_url(image_url))
        except ImageURLResolutionError:
            raise ImageTransportError("image URL was invalid") from None

    redirects = 0
    timeout = ClientTimeout(total=float(timeout_seconds))
    try:
        async with asyncio.timeout(float(timeout_seconds)):
            while True:
                async with _image_request(session, current_url, timeout=timeout) as response:
                    status = response.status
                    if status in REDIRECT_STATUSES:
                        if redirects >= max_redirects:
                            raise ImageTransportError("image redirect limit exceeded")
                        current_url = _redirect_target(
                            current_url, response.headers.get("Location")
                        )
                        redirects += 1
                        continue
                    if not 200 <= status < 300:
                        raise ImageTransportError("image request returned an unexpected status")
                    body = await _read_limited_body(response, max_image_bytes)
                    _validate_image_bytes(body, response.headers)
                    return ImageResponse(
                        url=current_url,
                        status=status,
                        headers=dict(response.headers),
                        body=body,
                    )
    except asyncio.CancelledError:
        raise
    except ImageTransportError:
        raise
    except (ImageURLResolutionError, asyncio.TimeoutError):
        raise ImageTransportError("image request failed") from None
    except Exception:
        raise ImageTransportError("image request failed") from None


def image_origin(url: str) -> ImageOrigin:
    """Return the normalized protocol, hostname, and effective port."""
    parsed = _parse_http_url(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if not hostname:
        raise ImageURLResolutionError
    return ImageOrigin(
        scheme=scheme,
        hostname=hostname.lower(),
        port=parsed.port or (443 if scheme == "https" else 80),
    )


@dataclass(frozen=True, slots=True)
class ImageNetworkPolicy:
    """Connection policy for approved private image origins."""

    allowed_private_origins: frozenset[ImageOrigin]

    @classmethod
    def from_urls(
        cls, larapaper_base_url: str, image_base_url: str | None = None
    ) -> ImageNetworkPolicy:
        """Approve only the configured Larapaper and image-base origins."""
        origins = {image_origin(larapaper_base_url)}
        if image_base_url is not None and image_base_url.strip():
            origins.add(image_origin(image_base_url))
        return cls(frozenset(origins))

    def allows_private_origin(self, url: str) -> bool:
        """Return whether a URL has an exact configured origin."""
        return image_origin(url) in self.allowed_private_origins


def _address_is_global(address: str) -> bool:
    """Classify a resolver address without trusting textual host syntax."""
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError as error:
        raise ImageSSRFError("resolver returned an invalid address") from error


class PolicyResolver(AbstractResolver):
    """Wrap an aiohttp resolver and enforce the DNS destination policy."""

    def __init__(
        self,
        resolver: AbstractResolver,
        *,
        allowed_private_origins: Iterable[ImageOrigin] = (),
        request_origin: ImageOrigin | None = None,
    ) -> None:
        self._resolver = resolver
        self._allowed_origins = frozenset(allowed_private_origins)
        self._request_origin = request_origin
        self._closed = False

    def validate(
        self,
        host: str,
        port: int,
        results: list[ResolveResult],
        *,
        request_origin: ImageOrigin | None = None,
    ) -> list[ResolveResult]:
        """Validate addresses for the URL currently being connected."""
        if not results:
            raise ImageSSRFError("resolver returned no addresses")
        addresses = [result.get("host") for result in results]
        if any(not isinstance(address, str) for address in addresses):
            raise ImageSSRFError("resolver returned an invalid address")
        global_flags = [_address_is_global(address) for address in addresses]
        if any(global_flags) and not all(global_flags):
            raise ImageSSRFError("mixed global and private DNS answers")
        origin = request_origin or _REQUEST_ORIGIN.get() or self._request_origin
        normalized_host = host.strip("[]").lower()
        authority_allowed = (
            origin is not None
            and origin in self._allowed_origins
            and origin.hostname == normalized_host
            and origin.port == port
        )
        if not all(global_flags) and not authority_allowed:
            raise ImageSSRFError("private image destination is not approved")
        return results

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        """Resolve and reject private, mixed, or malformed destinations."""
        results = await self._resolver.resolve(host, port, family)
        return self.validate(host, port, results)

    async def close(self) -> None:
        """Close the wrapped resolver with aiohttp's async resolver contract."""
        if not self._closed:
            self._closed = True
            await self._resolver.close()


class PolicyConnector(TCPConnector):
    """Apply the request URL's scheme to every connection-time DNS check."""

    def __init__(self, policy_resolver: PolicyResolver) -> None:
        super().__init__(resolver=policy_resolver, use_dns_cache=False)
        self._policy_resolver = policy_resolver

    async def _create_direct_connection(
        self,
        req: Any,
        traces: list[Any],
        timeout: Any,
        *,
        client_error: type[Exception] = ClientConnectorError,
    ) -> tuple[Any, Any]:
        """Fence resolver authorization to this request and redirect hop."""
        token = _REQUEST_ORIGIN.set(image_origin(str(req.url)))
        try:
            return await super()._create_direct_connection(
                req, traces, timeout, client_error=client_error
            )
        finally:
            _REQUEST_ORIGIN.reset(token)

    async def _resolve_host(
        self, host: str, port: int, traces: list[Any] | None = None
    ) -> list[ResolveResult]:
        """Validate literal IPs too; aiohttp bypasses resolvers for those."""
        results = await super()._resolve_host(host, port, traces)
        return self._policy_resolver.validate(host, port, results)

    async def close(self) -> None:
        """Close both the connector and its policy resolver."""
        await super().close()
        await self._policy_resolver.close()


def create_image_connector(
    policy: ImageNetworkPolicy,
    *,
    resolver: AbstractResolver | None = None,
) -> PolicyConnector:
    """Create a no-DNS-cache connector using connection-time policy checks."""
    policy_resolver = PolicyResolver(
        resolver or DefaultResolver(),
        allowed_private_origins=policy.allowed_private_origins,
    )
    return PolicyConnector(policy_resolver)

IMAGE_CONVERSION_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class _AdmissionRequest:
    """One lightweight queued request for the shared image worker."""

    entry_id: str
    token: OperationToken
    future: asyncio.Future[None]


class _ImageAdmission:
    """Fair, cancellation-aware FIFO admission for image operations."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._owner: tuple[str, OperationToken] | None = None
        self._queue: deque[_AdmissionRequest] = deque()
        self._waiters: dict[str, _AdmissionRequest] = {}
        self._closed = False

    async def async_acquire(
        self, entry_id: str, token: OperationToken
    ) -> bool:
        """Acquire the shared slot or wait without retaining image bytes."""
        if self._closed:
            return False
        key = (entry_id, token)
        if self._owner is not None and self._owner[0] == entry_id:
            return False
        if self._owner is None and not self._queue:
            self._owner = key
            return True
        if entry_id in self._waiters:
            return False
        future = self._loop.create_future()
        request = _AdmissionRequest(entry_id, token, future)
        self._waiters[entry_id] = request
        self._queue.append(request)
        self._pump()
        try:
            await future
        except asyncio.CancelledError:
            self._remove(request)
            if self._owner == key:
                self.release(*key)
            raise
        return True

    def abandon(self, entry_id: str, token: OperationToken) -> None:
        """Remove one abandoned queued request synchronously."""
        request = self._waiters.get(entry_id)
        if request is not None and request.token == token:
            self._remove(request)

    def release(self, entry_id: str, token: OperationToken) -> None:
        """Release the slot and admit the oldest surviving waiter."""
        if self._owner != (entry_id, token):
            return
        self._owner = None
        self._pump()

    def close(self) -> None:
        """Cancel queued waiters during final Home Assistant shutdown."""
        if self._closed:
            return
        self._closed = True
        for request in tuple(self._waiters.values()):
            if not request.future.done():
                request.future.cancel()
        self._waiters.clear()
        self._queue.clear()

    def _remove(self, request: _AdmissionRequest) -> None:
        if self._waiters.get(request.entry_id) is request:
            self._waiters.pop(request.entry_id, None)
        with contextlib.suppress(ValueError):
            self._queue.remove(request)
        if not request.future.done():
            request.future.cancel()
        self._pump()

    def _pump(self) -> None:
        if self._owner is not None or self._closed:
            return
        while self._queue:
            request = self._queue.popleft()
            if self._waiters.get(request.entry_id) is not request:
                continue
            self._waiters.pop(request.entry_id, None)
            if request.future.cancelled():
                continue
            self._owner = (request.entry_id, request.token)
            request.future.set_result(None)
            return


_AdmissionKey = tuple[str, "OperationToken"]


class ImageResources:
    """Own domain-scoped image transport and conversion resources."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        session: Any,
        executor: ThreadPoolExecutor,
        policy: ImageNetworkPolicy,
    ) -> None:
        self.hass = hass
        self.session = session
        self.executor = executor
        self.policy = policy
        self._loop = asyncio.get_running_loop()
        self._admission = _ImageAdmission(self._loop)
        self._active_key: _AdmissionKey | None = None
        self._conversion_future: Future[bytes] | None = None
        self._conversion_key: _AdmissionKey | None = None
        self._closed = False
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_stop
        )

    @classmethod
    async def async_create(
        cls,
        hass: HomeAssistant,
        policy: ImageNetworkPolicy,
    ) -> ImageResources:
        """Create the one domain-scoped session and executor."""
        connector = create_image_connector(policy)
        session = ClientSession(
            connector=connector,
            cookie_jar=DummyCookieJar(),
            raise_for_status=False,
        )
        return cls(
            hass,
            session=session,
            executor=ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="larapaper-image"
            ),
            policy=policy,
        )

    @property
    def closed(self) -> bool:
        """Return whether final Home Assistant shutdown has started."""
        return self._closed

    async def async_acquire(
        self, entry_id: str, token: OperationToken
    ) -> bool:
        """Acquire fair shared admission before fetching image bytes."""
        acquired = await self._admission.async_acquire(entry_id, token)
        if acquired:
            self._active_key = (entry_id, token)
        return acquired

    def abandon(self, entry_id: str, token: OperationToken) -> None:
        """Remove a queued request without releasing active conversion."""
        self._admission.abandon(entry_id, token)

    def release(self, entry_id: str, token: OperationToken) -> None:
        """Release admission when no conversion future owns the slot."""
        self._release_admission((entry_id, token))

    def _release_admission(self, key: _AdmissionKey) -> None:
        if self._active_key == key:
            self._active_key = None
        self._admission.release(*key)

    def submit_conversion(
        self,
        function: Callable[..., bytes],
        *args: Any,
        **kwargs: Any,
    ) -> Future[bytes] | None:
        """Submit one conversion without ever queueing a second payload."""
        if (
            self._closed
            or self._active_key is None
            or self._conversion_future is not None
        ):
            return None
        try:
            future = self.executor.submit(function, *args, **kwargs)
        except RuntimeError:
            return None
        self._conversion_future = future
        self._conversion_key = self._active_key
        future.add_done_callback(self._conversion_done)
        return future

    def _conversion_done(self, future: Future[bytes]) -> None:
        """Release admission only after the worker future really settles."""
        try:
            self._loop.call_soon_threadsafe(self._release_conversion, future)
        except RuntimeError:
            # The event loop is closing; no HA-owned state may be mutated here.
            return

    def _release_conversion(self, future: Future[bytes]) -> None:
        """Release the active conversion slot on the Home Assistant loop."""
        if self._conversion_future is future:
            self._conversion_future = None
            key = self._conversion_key
            self._conversion_key = None
            if key is not None:
                self._release_admission(key)

    async def _async_stop(self, _event: Any) -> None:
        """Close the session and abandon, rather than wait for, workers."""
        if self._closed:
            return
        self._closed = True
        self._admission.close()
        try:
            close_result = self.session.close()
            if inspect.isawaitable(close_result):
                await close_result
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)


async def async_get_image_resources(
    hass: HomeAssistant,
    *,
    larapaper_base_url: str,
    image_base_url: str | None = None,
    session: Any | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> ImageResources:
    """Return resources reused by every config-entry reload."""
    from .runtime import RuntimeHolder

    holder = RuntimeHolder.for_hass(hass)
    policy = ImageNetworkPolicy.from_urls(
        larapaper_base_url, image_base_url
    )
    resources = holder.image_resources
    if resources is not None:
        if resources.closed:
            raise RuntimeError("image resources are closed")
        if resources.policy != policy:
            raise RuntimeError(
                "image network policy cannot change before final stop"
            )
        return resources

    if session is None:
        connector = create_image_connector(policy)
        session = ClientSession(
            connector=connector,
            cookie_jar=DummyCookieJar(),
            raise_for_status=False,
        )
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="larapaper-image"
        )

    resources = ImageResources(
        hass,
        session=session,
        executor=executor,
        policy=policy,
    )
    holder.image_resources = resources
    return resources


class BoundedImageOperation:
    """Implement the scheduler's fetch, validation, and conversion seam."""

    def __init__(
        self,
        resources: ImageResources,
        *,
        entry_id: str,
        larapaper_base_url: str,
        image_base_url: str | None = None,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        self.resources = resources
        self.entry_id = entry_id
        self.larapaper_base_url = larapaper_base_url
        self.image_base_url = image_base_url
        self.max_image_bytes = max_image_bytes
        self._abandoned_through: tuple[int, int] | None = None

    def abandon(self, token: OperationToken) -> None:
        """Abandon logical work without releasing a running conversion slot."""
        marker = (token.lifecycle_epoch, token.cycle_generation)
        self.resources.abandon(self.entry_id, token)
        if self._abandoned_through is None or marker > self._abandoned_through:
            self._abandoned_through = marker

    def _is_abandoned(self, token: OperationToken) -> bool:
        """Reject every token at or before the latest abandoned generation."""
        marker = self._abandoned_through
        return marker is not None and (
            token.lifecycle_epoch,
            token.cycle_generation,
        ) <= marker

    def _fallback_url(self, url: str) -> str:
        try:
            return resolve_image_url(
                url,
                larapaper_base_url=self.larapaper_base_url,
                image_base_url=self.image_base_url,
            )
        except ImageURLResolutionError:
            return url

    async def async_process(
        self, url: str, token: OperationToken
    ) -> ImageOutcome:
        """Fetch one image and convert it without blocking the HA loop."""
        from .scheduler import ImageOutcome

        resolved_url = self._fallback_url(url)
        if self._is_abandoned(token):
            raise asyncio.CancelledError
        acquired = await self.resources.async_acquire(self.entry_id, token)
        if not acquired:
            return ImageOutcome(error_code="conversion", resolved_url=resolved_url)

        future: Future[bytes] | None = None
        try:
            if self._is_abandoned(token):
                raise asyncio.CancelledError
            try:
                response = await async_fetch_image(
                    self.resources.session,
                    url,
                    larapaper_base_url=self.larapaper_base_url,
                    image_base_url=self.image_base_url,
                    max_image_bytes=self.max_image_bytes,
                )
            except asyncio.CancelledError:
                raise
            except ImageValidationError:
                return ImageOutcome(error_code="validation", resolved_url=resolved_url)
            except ImageTransportError:
                return ImageOutcome(error_code="fetch", resolved_url=resolved_url)
            except Exception:
                return ImageOutcome(error_code="fetch", resolved_url=resolved_url)

            resolved_url = response.url
            if self._is_abandoned(token):
                raise asyncio.CancelledError
            future = self.resources.submit_conversion(
                convert_image_to_png,
                response.body,
                max_image_bytes=self.max_image_bytes,
            )
            if future is None:
                return ImageOutcome(error_code="conversion", resolved_url=resolved_url)

            try:
                async with asyncio.timeout(IMAGE_CONVERSION_TIMEOUT_SECONDS):
                    converted = await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return ImageOutcome(error_code="conversion", resolved_url=resolved_url)
            except ImageValidationError:
                return ImageOutcome(error_code="validation", resolved_url=resolved_url)
            except ImageConversionError:
                return ImageOutcome(error_code="conversion", resolved_url=resolved_url)
            except Exception:
                return ImageOutcome(error_code="conversion", resolved_url=resolved_url)

            if self._is_abandoned(token):
                raise asyncio.CancelledError
            return ImageOutcome(
                png_bytes=bytes(converted), resolved_url=resolved_url
            )
        finally:
            if future is None:
                self.resources.release(self.entry_id, token)


async def async_create_image_operation(
    hass: HomeAssistant,
    *,
    entry_id: str,
    larapaper_base_url: str,
    image_base_url: str | None = None,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> BoundedImageOperation:
    """Create an operation backed by the domain-scoped resources."""
    resources = await async_get_image_resources(
        hass,
        larapaper_base_url=larapaper_base_url,
        image_base_url=image_base_url,
    )
    return BoundedImageOperation(
        resources,
        entry_id=entry_id,
        larapaper_base_url=larapaper_base_url,
        image_base_url=image_base_url,
        max_image_bytes=max_image_bytes,
    )
