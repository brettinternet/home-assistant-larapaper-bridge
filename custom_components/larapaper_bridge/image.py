"""Image URL resolution and connection-time network policy."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from aiohttp import TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.client_exceptions import ClientConnectorError
from aiohttp.resolver import DefaultResolver




class ImageURLResolutionError(ValueError):
    """Raised when an image URL cannot be safely resolved."""


class ImageSSRFError(OSError):
    """Raised when DNS results do not satisfy the image-origin policy."""


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
