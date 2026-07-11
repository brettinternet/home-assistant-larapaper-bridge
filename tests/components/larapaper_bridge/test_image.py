"""Focused tests for image URL resolution."""

from __future__ import annotations

import pytest

from custom_components.larapaper_bridge.image import (
    ImageURLResolutionError,
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
        "ftp://cdn.example/screen.png",
        "https://user:secret@cdn.example/screen.png",
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
        "https://larapaper.example/bridge/?query=yes",
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
        "https://images.example/?query=yes",
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
