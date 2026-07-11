"""Focused tests for the first settlement-anchored scheduler slice."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from custom_components.larapaper_bridge.client import DisplayResult
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

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        self.calls.append((mac, api_key))
        self.clock.value += self.settle_delay
        return DisplayResult("/image.png", 60.0)


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
