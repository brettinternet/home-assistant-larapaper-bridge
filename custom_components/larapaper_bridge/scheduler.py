"""Display-cycle scheduling contracts and the first cycle implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from .client import DisplayResult, LarapaperClientError
from .const import CONF_MAX_STALE_SECONDS, CONF_MIN_POLL_SECONDS
from .runtime import EntryRuntime

ImageErrorCode = Literal["fetch", "validation", "conversion"]
Status = Literal["ready", "starting", "stale", "error"]

_ALLOWED_ERROR_CODES = frozenset(
    {
        "setup_auto_assign_disabled",
        "setup_failed",
        "display_failed",
        "invalid_display_response",
        "image_url_missing",
        "image_fetch_failed",
        "image_validation_failed",
        "image_conversion_failed",
        "internal_error",
    }
)
_IMAGE_ERROR_CODES = {
    "fetch": "image_fetch_failed",
    "validation": "image_validation_failed",
    "conversion": "image_conversion_failed",
}


def _safe_error_code(code: object) -> str:
    """Collapse unexpected classifications to the safe public fallback."""
    return code if isinstance(code, str) and code in _ALLOWED_ERROR_CODES else "internal_error"

UTCNow = Callable[[], datetime]



@dataclass(frozen=True, slots=True)
class OperationToken:
    """Identify one loaded-entry lifetime and one display cycle."""

    lifecycle_epoch: int
    cycle_generation: int


@dataclass(frozen=True, slots=True)
class ImageOutcome:
    """Immutable result returned by one image operation."""

    png_bytes: bytes | None = None
    resolved_url: str | None = None
    error_code: ImageErrorCode | None = None

    def __post_init__(self) -> None:
        if self.png_bytes is not None and not isinstance(self.png_bytes, bytes):
            raise TypeError("png_bytes must be immutable bytes")
        has_image = self.png_bytes is not None
        has_error = self.error_code is not None
        if has_image == has_error:
            raise ValueError("ImageOutcome must contain exactly one success or error")
        if has_error and not self.resolved_url:
            raise ValueError("failed image outcomes require a resolved URL")


class ImageOperation(Protocol):
    """Typed seam implemented by the bounded image pipeline."""

    async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
        """Fetch, validate, and convert one image."""

    def abandon(self, token: OperationToken) -> None:
        """Abandon work without waiting for a worker to finish."""


@dataclass(frozen=True, slots=True)
class CacheRecord:
    """Immutable last-good image projection."""

    png_bytes: bytes | None = None
    received_monotonic: float | None = None
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.png_bytes is not None and not isinstance(self.png_bytes, bytes):
            raise TypeError("png_bytes must be immutable bytes")
        if self.png_bytes is None:
            if self.received_monotonic is not None or self.received_at is not None:
                raise ValueError("an empty cache cannot have receipt metadata")
        elif self.received_monotonic is None or self.received_at is None:
            raise ValueError("a cached image requires receipt metadata")


@dataclass(frozen=True, slots=True)
class DiagnosticsState:
    """Redacted immutable scheduler status projection."""

    status: Status = "starting"
    ready: bool = False
    stale: bool = False
    last_success_at: datetime | None = None
    last_success_age_seconds: int | None = None
    last_error: str | None = None
    next_display_at: datetime | None = None
    next_retry_at: datetime | None = None


class DisplayClient(Protocol):
    """Minimal display client required by the scheduler."""

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        """Run one Larapaper display cycle."""


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class DisplayScheduler:
    """Run one display call per cycle using settlement-anchored deadlines."""

    def __init__(
        self,
        runtime: EntryRuntime,
        *,
        api_key: str,
        display_client: DisplayClient | None = None,
        image_operation: ImageOperation | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        utc_now: UTCNow = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.runtime = runtime
        self.api_key = api_key
        self.display_client = display_client or runtime.client
        self.image_operation = image_operation
        self.clock = clock
        self.sleep = sleep
        self.utc_now = utc_now
        self.max_stale_seconds = int(
            runtime.config_entry.data.get(CONF_MAX_STALE_SECONDS, 3600)
        )
        self.next_display_deadline: float | None = None
        self.last_display_result: DisplayResult | None = None
        self.last_error: str | None = None
        self.cache_record = CacheRecord()
        self._last_effective_interval = float(
            runtime.config_entry.data[CONF_MIN_POLL_SECONDS]
        )
        self._task: asyncio.Task[None] | None = None

    async def async_run_cycle(self) -> DisplayResult | None:
        """Run one display request and settle its next deadline."""
        token = self.runtime.begin_cycle()
        try:
            result = await self.display_client.async_display(
                self.runtime.mac, self.api_key
            )
        except asyncio.CancelledError:
            raise
        except LarapaperClientError as error:
            settled_at = self.clock()
            if not self.runtime.is_token_current(token):
                return None
            self.last_error = _safe_error_code(error.code)
            self.next_display_deadline = settled_at + self._last_effective_interval
            return None

        settled_at = self.clock()
        if not self.runtime.is_token_current(token):
            return None
        self.last_display_result = result
        self._last_effective_interval = result.effective_interval_seconds
        self.next_display_deadline = settled_at + self._last_effective_interval

        if result.image_url is None:
            if self.runtime.is_token_current(token):
                self.last_error = "image_url_missing"
        elif self.image_operation is not None:
            outcome = await self.image_operation.async_process(result.image_url, token)
            if self.runtime.is_token_current(token):
                if outcome.png_bytes is not None:
                    self.cache_record = CacheRecord(
                        png_bytes=outcome.png_bytes,
                        received_monotonic=self.clock(),
                        received_at=self.utc_now(),
                    )
                elif outcome.error_code is not None:
                    self.last_error = _IMAGE_ERROR_CODES.get(
                        outcome.error_code, "internal_error"
                    )
        return result

    def is_cache_fresh(self, now: float | None = None) -> bool:
        """Return whether the last-good image is strictly inside its stale limit."""
        record = self.cache_record
        if record.png_bytes is None or record.received_monotonic is None:
            return False
        current = self.clock() if now is None else now
        return current - record.received_monotonic < self.max_stale_seconds

    def cached_image(self, now: float | None = None) -> bytes | None:
        """Return cached PNG bytes only while the cache is fresh."""
        if not self.is_cache_fresh(now):
            return None
        return self.cache_record.png_bytes

    def diagnostics_state(self, now: float | None = None) -> DiagnosticsState:
        """Return a redacted, monotonic-clock-backed status projection."""
        current = self.clock() if now is None else now
        now_utc = self.utc_now()
        record = self.cache_record
        has_cache = record.png_bytes is not None and record.received_monotonic is not None
        age = (
            max(0.0, current - record.received_monotonic)
            if has_cache and record.received_monotonic is not None
            else None
        )
        fresh = age is not None and age < self.max_stale_seconds
        stale = has_cache and not fresh
        if fresh:
            status: Status = "ready"
        elif stale:
            status = "stale"
        elif self.last_error is not None:
            status = "error"
        else:
            status = "starting"
        next_display_at = (
            now_utc + timedelta(seconds=self.next_display_deadline - current)
            if self.next_display_deadline is not None
            else None
        )
        return DiagnosticsState(
            status=status,
            ready=fresh,
            stale=stale,
            last_success_at=record.received_at if has_cache else None,
            last_success_age_seconds=int(age) if age is not None else None,
            last_error=self.last_error,
            next_display_at=next_display_at,
            next_retry_at=None,
        )

    def async_start(self) -> asyncio.Task[None]:
        """Start the loop; its first display call is scheduled immediately."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = self.runtime.create_task(self._run())
        return self._task

    async def _run(self) -> None:
        while self.runtime.is_current():
            await self.async_run_cycle()
            if not self.runtime.is_current() or self.next_display_deadline is None:
                return
            await self.sleep(max(0.0, self.next_display_deadline - self.clock()))

    def stop(self) -> None:
        """Cancel the scheduler loop without waiting for display I/O."""
        if self._task is not None:
            self._task.cancel()


__all__ = [
    "CacheRecord",
    "DiagnosticsState",
    "DisplayScheduler",
    "ImageErrorCode",
    "ImageOperation",
    "ImageOutcome",
    "OperationToken",
]
