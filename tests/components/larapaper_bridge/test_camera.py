"""Focused tests for the cache-only camera projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from custom_components.larapaper_bridge.camera import (
    LarapaperBridgeCamera,
    async_setup_entry,
)
from custom_components.larapaper_bridge.runtime import RuntimeHolder
from custom_components.larapaper_bridge.scheduler import CacheRecord, DisplayScheduler

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


class FakeStore:
    async def async_load(self):
        return None


class FakeClient:
    async def async_setup(self, _mac: str):
        raise AssertionError("provisioning is not part of camera projection tests")

    async def async_display(self, _mac: str, _api_key: str):
        raise AssertionError("camera reads must not call /api/display")


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def camera_runtime(hass):
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry("entry-1", ENTRY_DATA),
        store=FakeStore(),
        client=FakeClient(),
    )
    clock = FakeClock()
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret-api-key",
        clock=clock,
        utc_now=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    yield runtime, scheduler, clock
    runtime.holder.invalidate()


@pytest.mark.asyncio
async def test_camera_is_cold_and_unavailable_without_scheduler_image(camera_runtime):
    runtime, _scheduler, _clock = camera_runtime
    camera = LarapaperBridgeCamera(runtime)

    assert camera.available is False
    assert camera.content_type == "image/png"
    assert await camera.async_camera_image() is None


@pytest.mark.asyncio
async def test_camera_returns_same_fresh_png_without_side_effects(camera_runtime):
    runtime, scheduler, clock = camera_runtime
    payload = b"immutable-png"
    received_at = datetime(2026, 7, 11, tzinfo=timezone.utc)
    scheduler.cache_record = CacheRecord(
        png_bytes=payload,
        received_monotonic=100.0,
        received_at=received_at,
    )
    scheduler.last_error = "display_failed"
    camera = LarapaperBridgeCamera(runtime)
    before = (scheduler.cache_record, scheduler.last_error, runtime.cycle_generation)

    first = await camera.async_camera_image()
    second = await camera.async_camera_image(width=320, height=240)

    assert camera.available is True
    assert first is payload
    assert second is payload
    assert (scheduler.cache_record, scheduler.last_error, runtime.cycle_generation) == before
    assert clock.value == 100.0


@pytest.mark.asyncio
async def test_camera_is_unavailable_and_returns_none_at_stale_boundary(camera_runtime):
    runtime, scheduler, clock = camera_runtime
    scheduler.cache_record = CacheRecord(
        png_bytes=b"stale-png",
        received_monotonic=100.0,
        received_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    camera = LarapaperBridgeCamera(runtime)
    clock.value = 110.0

    assert camera.available is False
    assert await camera.async_camera_image() is None


@pytest.mark.asyncio
async def test_camera_entity_setup_uses_existing_entry_runtime(hass, camera_runtime):
    runtime, _scheduler, _clock = camera_runtime
    added: list[LarapaperBridgeCamera] = []

    def add_entities(entities: list[LarapaperBridgeCamera]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, runtime.config_entry, add_entities)

    assert len(added) == 1
    assert isinstance(added[0], LarapaperBridgeCamera)
    assert runtime.camera_entity is added[0]
