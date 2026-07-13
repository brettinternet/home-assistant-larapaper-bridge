"""Focused tests for the first settlement-anchored scheduler slice."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    entry_id: str
    data: dict[str, object]
    title: str = MAC


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
        self.image_url: str | None = "/image.png"

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        self.calls.append((mac, api_key))
        self.clock.value += self.settle_delay
        if self.failure is not None:
            raise self.failure
        return DisplayResult(self.image_url, 60.0)
 
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
class FakeCameraEntity:
    def __init__(self) -> None:
        self.state_writes = 0

    def async_write_ha_state(self) -> None:
        self.state_writes += 1


class SequenceImage:
    def __init__(self, outcomes: list[ImageOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, OperationToken]] = []
        self.abandoned: list[OperationToken] = []

    async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
        self.calls.append((url, token))
        return self.outcomes.pop(0)

    def abandon(self, token: OperationToken) -> None:
        self.abandoned.append(token)


class BlockingImage:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.abandoned: list[OperationToken] = []

    async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
        self.started.set()
        await self.release.wait()
        return ImageOutcome(png_bytes=b"late", resolved_url=url)

    def abandon(self, token: OperationToken) -> None:
        self.abandoned.append(token)
class RetryBlockingImage:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, OperationToken]] = []
        self.abandoned: list[OperationToken] = []
        self.attempt = 0

    async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
        self.attempt += 1
        self.calls.append((url, token))
        if self.attempt == 1:
            return ImageOutcome(error_code="fetch", resolved_url=url)
        self.started.set()
        await self.release.wait()
        return ImageOutcome(png_bytes=b"late", resolved_url=url)

    def abandon(self, token: OperationToken) -> None:
        self.abandoned.append(token)




@pytest.fixture
def runtime(hass):
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry("entry-1", ENTRY_DATA), store=FakeStore(), client=FakeRuntimeClient()
    )
    yield runtime
    runtime.holder.invalidate_entry("entry-1")


@pytest.mark.asyncio
async def test_entry_schedulers_are_isolated(hass):
    holder = RuntimeHolder.for_hass(hass)
    first_entry = FakeEntry("entry-1", ENTRY_DATA)
    second_entry = FakeEntry(
        "entry-2",
        {**ENTRY_DATA, "mac": "11:22:33:44:55:66"},
    )
    first_runtime = holder.create_entry_runtime(
        first_entry, store=FakeStore(), client=FakeRuntimeClient()
    )
    second_runtime = holder.create_entry_runtime(
        second_entry, store=FakeStore(), client=FakeRuntimeClient()
    )
    first_clock = FakeClock()
    second_clock = FakeClock()
    first_display = FakeDisplay(first_clock)
    second_display = FakeDisplay(second_clock)
    first_scheduler = DisplayScheduler(
        first_runtime,
        api_key="first",
        display_client=first_display,
        clock=first_clock,
    )
    second_scheduler = DisplayScheduler(
        second_runtime,
        api_key="second",
        display_client=second_display,
        clock=second_clock,
    )

    await first_scheduler.async_run_cycle()

    assert first_display.calls == [(MAC, "first")]
    assert first_runtime.cycle_generation == 1
    assert second_display.calls == []
    assert second_runtime.cycle_generation == 0
    assert second_scheduler.cache_record == CacheRecord()

    holder.invalidate_entry(first_entry, expected_runtime=first_runtime)
    assert second_runtime.is_current()
    await second_scheduler.async_run_cycle()
    assert second_display.calls == [("11:22:33:44:55:66", "second")]
    holder.invalidate_entry(second_entry, expected_runtime=second_runtime)


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
async def test_cache_publication_and_stale_boundary_notify_camera(runtime):
    clock = FakeClock()
    camera = FakeCameraEntity()
    runtime.camera_entity = camera
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=FakeDisplay(clock),
        image_operation=FakeImage(clock),
        clock=clock,
    )

    await scheduler.async_run_cycle()
    assert camera.state_writes == 1

    clock.value = 110.0
    scheduler._notify_stale_cache(100.0)
    assert camera.state_writes == 2


@pytest.mark.asyncio
async def test_diagnostics_projection_uses_precedence_and_monotonic_age(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    received_at = datetime(2026, 7, 11, tzinfo=timezone.utc)
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=FakeImage(clock),
        clock=clock,
        utc_now=lambda: received_at,
    )

    await scheduler.async_run_cycle()
    scheduler.last_error = "display_failed"

    ready = scheduler.diagnostics_state(now=109.5)
    assert ready == DiagnosticsState(
        status="ready",
        ready=True,
        stale=False,
        last_success_at=received_at,
        last_success_age_seconds=9,
        last_error="display_failed",
        next_display_at=received_at + timedelta(seconds=50.5),
        next_retry_at=None,
    )

    stale = scheduler.diagnostics_state(now=110.0)
    assert stale.status == "stale"
    assert stale.ready is False
    assert stale.stale is True
    assert stale.last_success_age_seconds == 10
    assert stale.last_error == "display_failed"

@pytest.mark.asyncio
async def test_image_retry_reuses_captured_url_and_stops_at_display_deadline(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    image = SequenceImage(
        [
            ImageOutcome(error_code="fetch", resolved_url="https://cdn.test/a.png"),
            ImageOutcome(error_code="validation", resolved_url="https://cdn.test/a.png"),
        ]
    )
    gates = [asyncio.Event(), asyncio.Event()]
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await gates[len(sleep_calls) - 1].wait()

    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=image,
        clock=clock,
        sleep=sleep,
    )

    await scheduler.async_run_cycle()
    scheduler.next_display_deadline = 110.0
    await asyncio.sleep(0)
    assert scheduler.next_retry_deadline == 105.0

    clock.value = 105.0
    gates[0].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert image.calls == [
        ("/image.png", OperationToken(0, 1)),
        ("https://cdn.test/a.png", OperationToken(0, 1)),
    ]
    assert sleep_calls == [5.0]
    assert scheduler.next_retry_deadline is None
    assert scheduler.diagnostics_state().last_error == "image_validation_failed"


@pytest.mark.asyncio
async def test_unload_abandons_blocked_image_and_rejects_late_publication(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    image = BlockingImage()
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=image,
        clock=clock,
    )

    cycle = asyncio.create_task(scheduler.async_run_cycle())
    await image.started.wait()
    runtime.holder.invalidate_entry("entry-1")
    assert image.abandoned == [OperationToken(0, 1)]

    image.release.set()
    assert await cycle == DisplayResult("/image.png", 60.0)
    assert scheduler.cache_record == CacheRecord()


@pytest.mark.asyncio
async def test_next_cycle_abandons_blocked_retry_before_new_display(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    image = RetryBlockingImage()
    retry_gate = asyncio.Event()

    async def sleep(delay: float) -> None:
        await retry_gate.wait()

    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=image,
        clock=clock,
        sleep=sleep,
    )

    await scheduler.async_run_cycle()
    await asyncio.sleep(0)
    clock.value = 105.0
    retry_gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await image.started.wait()

    scheduler.next_display_deadline = 110.0
    clock.value = 110.0
    await scheduler.async_run_cycle(wait_for_image=False)

    assert display.calls == [(MAC, "secret"), (MAC, "secret")]
    assert image.abandoned == [OperationToken(0, 1)]


@pytest.mark.asyncio
async def test_missing_image_url_records_safe_error_without_image_work(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    display.image_url = None
    image = FakeImage(clock)
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, image_operation=image, clock=clock
    )

    await scheduler.async_run_cycle()

    assert image.calls == []
    assert scheduler.diagnostics_state().status == "error"
    assert scheduler.diagnostics_state().last_error == "image_url_missing"


@pytest.mark.asyncio
async def test_diagnostics_collapses_unknown_client_error(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    display.failure = LarapaperClientError("secret-leak", "unsafe")
    scheduler = DisplayScheduler(
        runtime, api_key="secret", display_client=display, clock=clock
    )

    await scheduler.async_run_cycle()

    assert scheduler.diagnostics_state().last_error == "internal_error"


@pytest.mark.asyncio
async def test_diagnostics_collapses_unknown_image_error(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)

    class InvalidImage(FakeImage):
        async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
            return ImageOutcome(error_code="secret-leak", resolved_url=url)

    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=InvalidImage(clock),
        clock=clock,
    )

    await scheduler.async_run_cycle()

    assert scheduler.diagnostics_state().last_error == "internal_error"


@pytest.mark.asyncio
async def test_late_image_success_after_unload_cannot_publish_cache(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)

    class InvalidatingImage(FakeImage):
        async def async_process(self, url: str, token: OperationToken) -> ImageOutcome:
            self.runtime.holder.invalidate_entry("entry-1")
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
@pytest.mark.asyncio
async def test_stale_notification_chunks_large_durations_and_respects_boundary(
    runtime,
):
    clock = FakeClock(99_950.0)
    delays: list[float] = []
    release = asyncio.Event()

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await release.wait()

    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=FakeDisplay(clock),
        image_operation=FakeImage(clock),
        clock=clock,
        sleep=sleep,
    )
    scheduler.max_stale_seconds = 100_000
    scheduler._schedule_stale_notification(0.0)
    await asyncio.sleep(0)
    assert delays == [50.0]

    scheduler.max_stale_seconds = 10**309
    scheduler._schedule_stale_notification(99_950.0)
    await asyncio.sleep(0)
    assert delays == [50.0, 86_400.0]

    runtime.holder.invalidate_entry("entry-1")


class GateSleep:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self.started.set()
        await self.release.wait()


class BlockingDisplay:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def async_display(self, _mac: str, _api_key: str) -> DisplayResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return DisplayResult("/image.png", 60.0)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_manual_refresh_wakes_automatic_wait_and_resets_deadline(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    sleep = GateSleep()
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        clock=clock,
        sleep=sleep,
    )
    task = scheduler.async_start()
    await sleep.started.wait()
    assert display.calls == [(MAC, "secret")]
    assert scheduler.next_display_deadline == 160.0

    clock.value = 123.0
    await scheduler.async_request_refresh()
    for _ in range(8):
        await asyncio.sleep(0)
        if len(display.calls) == 2:
            break
    assert len(display.calls) == 2
    assert runtime.cycle_generation == 2
    assert scheduler.next_display_deadline == 183.0

    scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_manual_refresh_coalesces_with_active_display(runtime):
    display = BlockingDisplay()
    scheduler = DisplayScheduler(runtime, api_key="secret", display_client=display)
    task = scheduler.async_start()
    await display.started.wait()

    await asyncio.gather(
        scheduler.async_request_refresh(),
        scheduler.async_request_refresh(),
    )
    assert display.calls == 1
    assert display.max_active == 1

    display.release.set()
    await asyncio.sleep(0)
    scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_manual_timeout_uses_display_failure_settlement(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    sleep = GateSleep()
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        clock=clock,
        sleep=sleep,
    )
    task = scheduler.async_start()
    await sleep.started.wait()

    display.failure = TimeoutError()
    clock.value = 125.0
    await scheduler.async_request_refresh()
    for _ in range(8):
        await asyncio.sleep(0)
        if len(display.calls) == 2:
            break
    assert len(display.calls) == 2
    assert scheduler.last_error == "display_failed"

    scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_manual_refresh_abandons_prior_image_before_new_cycle(runtime):
    clock = FakeClock()
    display = FakeDisplay(clock)
    image = BlockingImage()
    sleep = GateSleep()
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret",
        display_client=display,
        image_operation=image,
        clock=clock,
        sleep=sleep,
    )
    task = scheduler.async_start()
    await image.started.wait()
    await sleep.started.wait()

    await scheduler.async_request_refresh()
    for _ in range(8):
        await asyncio.sleep(0)
        if len(display.calls) == 2:
            break
    assert len(display.calls) == 2
    assert image.abandoned == [OperationToken(0, 1)]
    assert runtime.cycle_generation == 2

    scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
