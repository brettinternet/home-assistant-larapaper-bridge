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


_STALE_NOTIFICATION_CHUNK_SECONDS = 24 * 60 * 60

class DisplayScheduler:
    """Run one display call per cycle using settlement-anchored deadlines."""

    _RETRY_DELAYS = (5.0, 10.0, 20.0, 40.0, 60.0)

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
        self.next_retry_deadline: float | None = None
        self.last_display_result: DisplayResult | None = None
        self.last_error: str | None = None
        self.cache_record = CacheRecord()
        self._last_effective_interval = float(
            runtime.config_entry.data[CONF_MIN_POLL_SECONDS]
        )
        self._task: asyncio.Task[None] | None = None
        self._image_task: asyncio.Task[None] | None = None
        self._image_token: OperationToken | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_token: OperationToken | None = None
        self._retry_url: str | None = None
        self._retry_attempt = 0
        self._stale_task: asyncio.Task[None] | None = None
        runtime.scheduler = self

    async def async_run_cycle(
        self, *, wait_for_image: bool = True
    ) -> DisplayResult | None:
        """Run one display request and settle its next deadline."""
        token = self._begin_cycle()
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
            self.last_error = "image_url_missing"
        elif self.image_operation is not None:
            if wait_for_image:
                self._image_token = token
                await self._process_image(result.image_url, token)
            else:
                self._image_token = token
                self._image_task = self.runtime.create_task(
                    self._process_image(result.image_url, token)
                )
        return result

    def _begin_cycle(self) -> OperationToken:
        """Advance generation before abandoning all prior cycle work."""
        token = self.runtime.begin_cycle()
        self._abandon_current_work()
        return token

    def _abandon_current_work(self) -> None:
        """Cancel logical work and synchronously abandon image processing."""
        image_token = self._image_token
        if self._image_task is not None:
            self._image_task.cancel()
        if image_token is not None and self.image_operation is not None:
            self.image_operation.abandon(image_token)
        if self._retry_task is not None:
            self._retry_task.cancel()
        self._image_task = None
        self._image_token = None
        self._retry_task = None
        self._retry_token = None
        self._retry_url = None
        self._retry_attempt = 0
        self.next_retry_deadline = None

    async def _process_image(self, url: str, token: OperationToken) -> None:
        """Process one URL and schedule only retries before the next cycle."""
        if not self.runtime.is_token_current(token) or self.image_operation is None:
            return
        try:
            outcome = await self.image_operation.async_process(url, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = ImageOutcome(error_code="conversion", resolved_url=url)
        if not self.runtime.is_token_current(token):
            return
        self._image_task = None
        self._image_token = None
        if outcome.png_bytes is not None:
            received_monotonic = self.clock()
            self.cache_record = CacheRecord(
                png_bytes=outcome.png_bytes,
                received_monotonic=received_monotonic,
                received_at=self.utc_now(),
            )
            self.last_error = None
            self._clear_retry()
            self._schedule_stale_notification(received_monotonic)
            self._notify_camera_state()
            return
        if outcome.error_code is not None:
            self.last_error = _IMAGE_ERROR_CODES.get(
                outcome.error_code, "internal_error"
            )
            self._schedule_retry(outcome.resolved_url, token)

    def _schedule_retry(self, url: str | None, token: OperationToken) -> None:
        """Schedule a captured-URL retry only before the display deadline."""
        if (
            not url
            or self.next_display_deadline is None
            or not self.runtime.is_token_current(token)
        ):
            return
        delay = self._RETRY_DELAYS[min(self._retry_attempt, len(self._RETRY_DELAYS) - 1)]
        due = self.clock() + delay
        if due >= self.next_display_deadline:
            return
        self._retry_attempt += 1
        self._retry_token = token
        self._retry_url = url
        self.next_retry_deadline = due
        self._retry_task = self.runtime.create_task(self._run_retry(token, url, due))

    async def _run_retry(
        self, token: OperationToken, url: str, due: float
    ) -> None:
        """Wait for and execute a captured-URL image retry."""
        try:
            await self.sleep(max(0.0, due - self.clock()))
            if (
                not self.runtime.is_token_current(token)
                or self.next_display_deadline is None
                or self.clock() >= self.next_display_deadline
                or self.image_operation is None
            ):
                return
            self.next_retry_deadline = None
            self._retry_task = None
            self._image_token = token
            self._image_task = asyncio.current_task()
            await self._process_image(url, token)
        finally:
            current_task = asyncio.current_task()
            if self._image_task is current_task:
                self._image_task = None
                self._image_token = None
            if self._retry_token == token and (
                self._retry_task is current_task or self._retry_task is None
            ):
                self._retry_token = None
                self._retry_url = None
                self._retry_task = None
                self.next_retry_deadline = None

    def _clear_retry(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
        self._retry_task = None
        self._retry_token = None
        self._retry_url = None
        self._retry_attempt = 0
        self.next_retry_deadline = None

    def _notify_camera_state(self) -> None:
        """Refresh the cache-backed camera projection on the HA loop."""
        self.runtime.notify_camera_state()

    def _schedule_stale_notification(self, received_monotonic: float) -> None:
        """Notify the camera when this cache record reaches its stale boundary."""
        if self._stale_task is not None:
            self._stale_task.cancel()
        age = max(0.0, self.clock() - received_monotonic)
        try:
            remaining = float(self.max_stale_seconds) - age
        except OverflowError:
            remaining = float("inf")
        delay = min(
            float(_STALE_NOTIFICATION_CHUNK_SECONDS),
            max(0.0, remaining),
        )
        self._stale_task = self.runtime.create_task(
            self._wait_for_stale_boundary(received_monotonic, delay)
        )

    async def _wait_for_stale_boundary(
        self, received_monotonic: float, delay: float
    ) -> None:
        """Wait on the scheduler's injected clock/sleep boundary."""
        try:
            await self.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            if self._stale_task is asyncio.current_task():
                self._stale_task = None
        self._notify_stale_cache(received_monotonic)

    def _notify_stale_cache(self, received_monotonic: float) -> None:
        """Refresh the camera only if the same cache record is now stale."""
        if not self.runtime.is_current():
            return
        record = self.cache_record
        if record.received_monotonic != received_monotonic:
            return
        if self.is_cache_fresh():
            self._schedule_stale_notification(received_monotonic)
            return
        self._notify_camera_state()

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
        next_retry_at = (
            now_utc + timedelta(seconds=self.next_retry_deadline - current)
            if self.next_retry_deadline is not None
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
            next_retry_at=next_retry_at,
        )

    def async_start(self) -> asyncio.Task[None]:
        """Start the loop; its first display call is scheduled immediately."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = self.runtime.create_task(self._run())
        return self._task

    async def _run(self) -> None:
        while self.runtime.is_current():
            await self.async_run_cycle(wait_for_image=False)
            if not self.runtime.is_current() or self.next_display_deadline is None:
                return
            await self.sleep(max(0.0, self.next_display_deadline - self.clock()))

    def stop(self) -> None:
        """Cancel the scheduler loop and abandon image work without waiting."""
        self._abandon_current_work()
        if self._stale_task is not None:
            self._stale_task.cancel()
            self._stale_task = None
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
