"""Image URL resolution contracts for the bounded image pipeline."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit, urlunsplit


class ImageURLResolutionError(ValueError):
    """Raised when an image URL cannot be safely resolved."""


def _parse_http_url(value: object, *, allow_relative: bool = False) -> SplitResult:
    """Parse an HTTP(S) URL without accepting credentials or fragments."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
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
    if base.query or base.fragment:
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
        if override.path not in ("", "/") or override.query or override.fragment:
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
