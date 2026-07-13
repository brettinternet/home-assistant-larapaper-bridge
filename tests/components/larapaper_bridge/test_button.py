"""Focused tests for the native manual refresh button."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from custom_components.larapaper_bridge.button import (
    LarapaperBridgeRefreshButton,
    async_setup_entry,
)
from custom_components.larapaper_bridge.client import DisplayResult
from custom_components.larapaper_bridge.const import DOMAIN
from custom_components.larapaper_bridge.runtime import RuntimeHolder
from custom_components.larapaper_bridge.scheduler import DisplayScheduler

MAC = "AA:BB:CC:DD:EE:FF"
ENTRY_DATA = {
    "base_url": "https://example.test/bridge/",
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


class FakeClient:
    async def async_setup(self, _mac: str):
        raise AssertionError("provisioning is not part of button tests")


class FakeDisplay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def async_display(self, mac: str, api_key: str) -> DisplayResult:
        self.calls.append((mac, api_key))
        return DisplayResult("/image.png", 60.0)


@pytest.fixture
def button_runtime(hass):
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry("entry-1", ENTRY_DATA, "friendly-id"),
        store=FakeStore(),
        client=FakeClient(),
    )
    yield runtime
    runtime.holder.invalidate_entry("entry-1")


@pytest.mark.asyncio
async def test_button_setup_uses_mac_identity_and_shared_device(button_runtime, hass):
    added: list[LarapaperBridgeRefreshButton] = []

    await async_setup_entry(hass, button_runtime.config_entry, added.extend)

    assert len(added) == 1
    button = added[0]
    assert button.name == "Refresh display"
    assert button.unique_id == f"{DOMAIN}_{MAC}_refresh_display"
    assert button.device_info == {
        "identifiers": {(DOMAIN, MAC)},
        "name": "friendly-id",
        "manufacturer": "Larapaper",
    }
    assert button.available is False
    assert button_runtime.button_entity is button


@pytest.mark.asyncio
async def test_button_delegates_only_to_scheduler(button_runtime):
    calls = 0

    class SpyScheduler:
        is_running = True

        async def async_request_refresh(self) -> None:
            nonlocal calls
            calls += 1


        def stop(self) -> None:
            return None
    button_runtime.scheduler = SpyScheduler()
    button = LarapaperBridgeRefreshButton(button_runtime)

    await button.async_press()

    assert calls == 1


@pytest.mark.asyncio
async def test_button_available_after_scheduler_start_and_press_is_entry_scoped(
    hass,
):
    holder = RuntimeHolder.for_hass(hass)
    first_runtime = holder.create_entry_runtime(
        FakeEntry("entry-1", ENTRY_DATA, "first"),
        store=FakeStore(),
        client=FakeClient(),
    )
    second_runtime = holder.create_entry_runtime(
        FakeEntry(
            "entry-2",
            {**ENTRY_DATA, "mac": "11:22:33:44:55:66"},
            "second",
        ),
        store=FakeStore(),
        client=FakeClient(),
    )
    first_display = FakeDisplay()
    second_display = FakeDisplay()
    first_scheduler = DisplayScheduler(
        first_runtime, api_key="first", display_client=first_display
    )
    second_scheduler = DisplayScheduler(
        second_runtime, api_key="second", display_client=second_display
    )
    first_button = LarapaperBridgeRefreshButton(first_runtime)
    second_button = LarapaperBridgeRefreshButton(second_runtime)
    first_task = first_scheduler.async_start()
    second_task = second_scheduler.async_start()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert first_button.available is True
        assert second_button.available is True
        assert first_display.calls == [(MAC, "first")]
        assert second_display.calls == [("11:22:33:44:55:66", "second")]

        await first_button.async_press()
        for _ in range(8):
            await asyncio.sleep(0)
            if len(first_display.calls) == 2:
                break

        assert len(first_display.calls) == 2
        assert len(second_display.calls) == 1

        holder.invalidate_entry(
            first_runtime.config_entry,
            expected_runtime=first_runtime,
        )
        assert first_button.available is False
        assert second_button.available is True
    finally:
        holder.invalidate_entry(
            first_runtime.config_entry,
            expected_runtime=first_runtime,
        )
        second_scheduler.stop()
        first_task.cancel()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
