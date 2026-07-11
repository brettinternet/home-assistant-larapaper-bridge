"""Image URL resolution and connection-time network policy."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
import re

from .const import DEFAULT_MAX_IMAGE_BYTES
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from aiohttp import ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.client_exceptions import ClientConnectorError
from aiohttp.resolver import DefaultResolver




class ImageURLResolutionError(ValueError):
    """Raised when an image URL cannot be safely resolved."""


class ImageSSRFError(OSError):
    """Raised when DNS results do not satisfy the image-origin policy."""


class ImageTransportError(OSError):
    """Raised when an image request cannot complete safely."""


@dataclass(frozen=True, slots=True)
class ImageResponse:
    """The final response returned by the image transport."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes



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
    if source.scheme or source.netloc:
        resolved = _normalized_absolute_url(source, scheme=source.scheme or base.scheme)
    else:
        path = source.path[1:] if source.path.startswith("/") else source.path
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
        raise ImageTransportError("image content type was invalid")
    for parameter in parts[1:]:
        name_value = parameter.strip().split("=", 1)
        if len(name_value) != 2 or not _TOKEN_RE.fullmatch(name_value[0].strip()):
            raise ImageTransportError("image content type was invalid")
        parameter_value = name_value[1].strip()
        if not (
            _TOKEN_RE.fullmatch(parameter_value)
            or _valid_quoted_parameter(parameter_value)
        ):
            raise ImageTransportError("image content type was invalid")
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
            raise ImageTransportError("image bytes did not match a supported format")
        return
    if media_type == "image/png":
        if not is_png:
            raise ImageTransportError("image bytes did not match PNG")
        return
    if media_type == "image/bmp":
        if not is_bmp:
            raise ImageTransportError("image bytes did not match BMP")
        return
    raise ImageTransportError("image content type was unsupported")

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
