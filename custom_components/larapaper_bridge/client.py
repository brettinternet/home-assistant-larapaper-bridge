"""One-shot Larapaper setup and display HTTP operations."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
from dataclasses import dataclass
from typing import Any, Literal

from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

SETUP_TIMEOUT_SECONDS = 10.0
DISPLAY_TIMEOUT_SECONDS = 10.0

ClientErrorCode = Literal[
    "setup_auto_assign_disabled",
    "setup_failed",
    "display_failed",
    "invalid_display_response",
]


class LarapaperClientError(Exception):
    """A safe, stable classification for a Larapaper client failure."""

    def __init__(self, code: ClientErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SetupCredentials:
    """Credentials returned by a successful setup request."""

    api_key: str
    friendly_id: str


@dataclass(frozen=True, slots=True)
class DisplayResult:
    """Validated result of one display-cycle request."""

    image_url: str | None
    effective_interval_seconds: float


class LarapaperClient:
    """Perform exactly one setup or display request per method call."""

    def __init__(
        self,
        hass: Any,
        base_url: str,
        minimum_poll_seconds: float,
    ) -> None:
        self._session = async_get_clientsession(hass)
        self._base_url = base_url.rstrip("/")
        self._minimum_poll_seconds = minimum_poll_seconds

    async def async_setup(self, mac: str) -> SetupCredentials:
        """Call setup once, refusing redirects and forwarding only ``ID``."""

        url = f"{self._base_url}/api/setup"
        try:
            async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
                async with self._request(
                    url,
                    headers={"ID": mac},
                    timeout=SETUP_TIMEOUT_SECONDS,
                ) as response:
                    if response.status == 404:
                        raise LarapaperClientError(
                            "setup_auto_assign_disabled",
                            "Larapaper setup returned 404; enable assign_new_devices",
                        )
                    if response.status < 200 or response.status >= 300:
                        raise LarapaperClientError(
                            "setup_failed", "Larapaper setup request returned an unexpected status"
                        )
                    body = await self._json(response)
        except asyncio.CancelledError:
            raise
        except LarapaperClientError:
            raise
        except Exception:
            raise LarapaperClientError("setup_failed", "Larapaper setup request failed") from None

        if not isinstance(body, dict):
            raise LarapaperClientError("setup_failed", "Larapaper setup response was not a JSON object")
        api_key = body.get("api_key")
        friendly_id = body.get("friendly_id")
        if not isinstance(api_key, str) or not api_key.strip():
            raise LarapaperClientError("setup_failed", "Larapaper setup response had invalid credentials")
        if not isinstance(friendly_id, str) or not friendly_id.strip():
            raise LarapaperClientError("setup_failed", "Larapaper setup response had invalid credentials")
        return SetupCredentials(api_key=api_key.strip(), friendly_id=friendly_id.strip())

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        """Call display once, refusing redirects and forwarding only API headers."""

        url = f"{self._base_url}/api/display"
        try:
            async with asyncio.timeout(DISPLAY_TIMEOUT_SECONDS):
                async with self._request(
                    url,
                    headers={"ID": mac, "Access-Token": api_key},
                    timeout=DISPLAY_TIMEOUT_SECONDS,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raise LarapaperClientError(
                            "display_failed", "Larapaper display request returned an unexpected status"
                        )
                    body = await self._json(response)
        except asyncio.CancelledError:
            raise
        except LarapaperClientError:
            raise
        except Exception:
            raise LarapaperClientError("display_failed", "Larapaper display request failed") from None

        if not isinstance(body, dict):
            raise LarapaperClientError(
                "invalid_display_response", "Larapaper display response was not a JSON object"
            )
        refresh_rate = body.get("refresh_rate")
        if isinstance(refresh_rate, bool) or not isinstance(refresh_rate, (int, float)):
            raise LarapaperClientError(
                "invalid_display_response", "Larapaper display response had an invalid refresh rate"
            )
        try:
            refresh_rate_value = float(refresh_rate)
        except (OverflowError, ValueError):
            raise LarapaperClientError(
                "invalid_display_response", "Larapaper display response had an invalid refresh rate"
            ) from None
        if not math.isfinite(refresh_rate_value) or refresh_rate_value <= 0:
            raise LarapaperClientError(
                "invalid_display_response", "Larapaper display response had an invalid refresh rate"
            )

        raw_image_url = body.get("image_url")
        if raw_image_url is not None and not isinstance(raw_image_url, str):
            raise LarapaperClientError(
                "invalid_display_response", "Larapaper display response had a non-string image URL"
            )
        image_url = raw_image_url or None
        return DisplayResult(
            image_url=image_url,
        effective_interval_seconds=max(refresh_rate_value, self._minimum_poll_seconds),
        )

    @contextlib.asynccontextmanager
    async def _request(self, url: str, *, headers: dict[str, str], timeout: float):
        """Yield a response while keeping redirects disabled."""

        request = self._session.get(
            url,
            headers=headers,
            allow_redirects=False,
            timeout=ClientTimeout(total=timeout),
        )
        if hasattr(request, "__aenter__"):
            async with request as response:
                yield response
            return
        if inspect.isawaitable(request):
            request = await request
        yield request

    @staticmethod
    async def _json(response: Any) -> Any:
        """Read a response body as JSON without exposing its contents on errors."""

        json_method = response.json
        try:
            return await json_method(content_type=None)
        except TypeError:
            return await json_method()


