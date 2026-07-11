"""Focused tests for the first settlement-anchored scheduler slice."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from custom_components.larapaper_bridge.client import DisplayResult, LarapaperClientError
from custom_components.larapaper_bridge.runtime import RuntimeHolder
from custom_components.larapaper_bridge.scheduler import (
    CacheRecord,
    DisplayScheduler,
    DiagnosticsState,
    ImageOutcome,
    OperationToken,
)

MAC = "AA:BB:CC:DD:EE:FF"
ENTRY_DATA = {
    "base_url": "https://example.test/",
    "mac": MAC,
    "min_poll_seconds": 60.0,
    "max_stale_seconds": 10,
}


@dataclass
class FakeEntry:
    data: dict[str, object]


class FakeStore:
    async def async_load(self):
        return None


class FakeRuntimeClient:
    async def async_setup(self, mac):
        raise AssertionError("setup is not part of scheduler tests")


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeDisplay:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.settle_delay = 0.0
        self.failure: LarapaperClientError | None = None

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        self.calls.append((mac, api_key))
        self.clock.value += self.settle_delay
        if self.failure is not None:
            raise self.failure
        return DisplayResult("/image.png", 60.0)
 
class FakeImage:
    def __init__(self, clock: FakeClock, payload: bytes = b"png") -> None:
        self.clock = clock
        self.payload = payload
        self.calls: list[tuple[str, OperationToken]] = []

    async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
        self.calls.append((url, token))
        return ImageOutcome(png_bytes=self.payload, resolved_url=url)

    def abandon(self, token: OperationToken) -> None:
        raise AssertionError("abandonment is not part of cache tests")


@pytest.fixture
def runtime(hass):
    return RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=FakeStore(), client=FakeRuntimeClient()
    )


@pytest.mark.asyncio
async def test_one_cycle_calls_display_once_and_anchors_deadline_at_settlement(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    display.settle_delay = 7.0
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, clock=clock
    )

    result = await scheduler.async_run_cycle()

    assert result == DisplayResult("/image.png", 60.0)
    assert display.calls == [(MAC, "secret")]
    assert runtime.cycle_generation == 1
    assert scheduler.next_display_deadline == 167.0


@pytest.mark.asyncio
async def test_image_success_replaces_cache_and_freshness_uses_monotonic_boundary(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    image = FakeImage(clock)
    received_at = datetime(2026, 7, 11, tzinfo=timezone.utc)
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=image,
        clock=clock,
        utc_now=lambda: received_at,
    )

    await scheduler.async_run_cycle()

    assert image.calls == [("/image.png", OperationToken(0, 1))]
    assert scheduler.cache_record == CacheRecord(
        png_bytes=b"png",
        received_monotonic=100.0,
        received_at=received_at,
    )
    assert scheduler.cached_image(109.999) == b"png"
    assert scheduler.is_cache_fresh(110.0) is False
    assert scheduler.cached_image(110.0) is None


@pytest.mark.asyncio
async def test_late_image_success_after_unload_cannot_publish_cache(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)

    class InvalidatingImage(FakeImage):
        async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
            self.runtime.holder.invalidate()
            return await super().async_process(url, token)

        def __init__(self, runtime):
            super().__init__(clock)
            self.runtime = runtime

    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=InvalidatingImage(runtime),
        clock=clock,
    )

    await scheduler.async_run_cycle()

    assert scheduler.cache_record == CacheRecord()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["invalid_display_response", "display_failed"])
async def test_display_failure_uses_prior_interval_and_records_error(runtime, error_code):
    clock = FakeClock()
    display = FakeDisplay(clock)
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, clock=clock
    )

    assert await scheduler.async_run_cycle() == DisplayResult("/image.png", 60.0)

    display.failure = LarapaperClientError(error_code, "safe failure")
    display.settle_delay = 7.0
    assert await scheduler.async_run_cycle() is None

    assert display.calls == [(MAC, "secret"), (MAC, "secret")]
    assert scheduler.last_error == error_code
    assert scheduler.next_display_deadline == 167.0


@pytest.mark.asyncio
async def test_scheduler_continues_after_display_failure(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, clock=clock
    )
    display.failure = LarapaperClientError("display_failed", "safe failure")
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) == 1:
            display.failure = None
            clock.value += delay
            return
        raise asyncio.CancelledError

    scheduler.sleep = sleep
    task = scheduler.async_start()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(display.calls) == 2
    assert sleep_calls == [60.0, 60.0]


@pytest.mark.asyncio
async def test_start_runs_first_cycle_immediately(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, clock=clock
    )

    task = scheduler.async_start()
    await asyncio.sleep(0)

    assert display.calls == [(MAC, "secret")]
    scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_image_outcome_and_projection_types_are_immutable():
    token = OperationToken(lifecycle_epoch=2, cycle_generation=3)
    outcome = ImageOutcome(png_bytes=b"png")
    cache = CacheRecord(
        png_bytes=b"png",
        received_monotonic=1.0,
        received_at=datetime.now(timezone.utc),
    )

    assert token == OperationToken(2, 3)
    assert outcome.resolved_url is None
    assert DiagnosticsState().status == "starting"
    assert cache.png_bytes == b"png"

    with pytest.raises(ValueError):
        ImageOutcome()
    with pytest.raises(ValueError):
        ImageOutcome(error_code="fetch")
