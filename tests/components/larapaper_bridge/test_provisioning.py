"""Provisioning retry and lifecycle-fencing tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from custom_components.larapaper_bridge.client import (
    LarapaperClientError,
    SetupCredentials,
)
from custom_components.larapaper_bridge.provisioning import (
    ProvisioningStateError,
    RETRY_DELAYS_SECONDS,
)
from custom_components.larapaper_bridge.runtime import RuntimeHolder

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
    def __init__(self, state=None):
        self.state = state
        self.saves: list[tuple[str, ...]] = []

    async def async_load(self):
        return self.state

    async def async_save_pending(self, mac):
        self.saves.append(("pending", mac))
        self.state = {"version": 1, "mac": mac}

    async def async_save_complete(self, mac, api_key, friendly_id):
        self.saves.append(("complete", mac, api_key, friendly_id))
        self.state = {
            "version": 1,
            "mac": mac,
            "api_key": api_key,
            "friendly_id": friendly_id,
        }


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def async_setup(self, mac):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSleep:
    def __init__(self):
        self.delays: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, delay):
        self.delays.append(delay)
        await self.release.wait()


@pytest.mark.asyncio
async def test_provisioning_persists_pending_before_setup_and_complete_after(hass):
    store = FakeStore()
    client = FakeClient([SetupCredentials("api-key", "friendly-id")])
    holder = RuntimeHolder.for_hass(hass)
    runtime = holder.create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )

    result = await runtime.async_provision()

    assert result == {
        "version": 1,
        "mac": MAC,
        "api_key": "api-key",
        "friendly_id": "friendly-id",
    }
    assert store.saves == [
        ("pending", MAC),
        ("complete", MAC, "api-key", "friendly-id"),
    ]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_complete_state_bypasses_setup(hass):
    store = FakeStore(
        {
            "version": 1,
            "mac": MAC,
            "api_key": "api-key",
            "friendly_id": "friendly-id",
        }
    )
    client = FakeClient([])
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )

    assert await runtime.async_provision() == store.state
    assert client.calls == 0
    assert store.saves == []


@pytest.mark.asyncio
async def test_retries_with_fixed_backoff_and_observes_complete_state(hass):
    store = FakeStore()
    client = FakeClient(
        [
            LarapaperClientError("setup_failed", "safe"),
            SetupCredentials("api-key", "friendly-id"),
        ]
    )
    sleep = FakeSleep()
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client, sleep=sleep
    )
    operation = asyncio.create_task(runtime.async_provision())
    for _ in range(3):
        await asyncio.sleep(0)
    assert sleep.delays == [RETRY_DELAYS_SECONDS[0]]
    sleep.release.set()

    assert await operation == store.state
    assert client.calls == 2
    assert store.saves == [
        ("pending", MAC),
        ("complete", MAC, "api-key", "friendly-id"),
    ]


@pytest.mark.asyncio
async def test_pending_state_is_reused_after_restart(hass):
    store = FakeStore({"version": 1, "mac": MAC})
    client = FakeClient([SetupCredentials("api-key", "friendly-id")])
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )

    await runtime.async_provision()

    assert store.saves == [("complete", MAC, "api-key", "friendly-id")]


@pytest.mark.asyncio
async def test_mismatched_persisted_mac_never_retries(hass):
    store = FakeStore({"version": 1, "mac": "11:22:33:44:55:66"})
    client = FakeClient([])
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )

    with pytest.raises(ProvisioningStateError):
        await runtime.async_provision()
    assert client.calls == 0
    assert store.saves == []


@pytest.mark.asyncio
async def test_unload_cancels_provisioning_and_advances_epoch(hass):
    store = FakeStore()
    client = FakeClient([LarapaperClientError("setup_failed", "safe")])
    sleep = FakeSleep()
    holder = RuntimeHolder.for_hass(hass)
    runtime = holder.create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client, sleep=sleep
    )
    operation = asyncio.create_task(runtime.async_provision())
    for _ in range(3):
        await asyncio.sleep(0)
    assert runtime.tasks
    old_epoch = holder.lifecycle_epoch

    holder.invalidate()

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert runtime.stopped is True
    assert holder.lifecycle_epoch == old_epoch + 1
    assert holder.current is None
    assert not runtime.tasks


@pytest.mark.asyncio
async def test_unload_cancels_registered_retry_handle(hass):
    store = FakeStore()
    client = FakeClient([LarapaperClientError("setup_failed", "safe")])
    holder = RuntimeHolder.for_hass(hass)
    runtime = holder.create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )
    operation = asyncio.create_task(runtime.async_provision())
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(runtime.retry_handles) == 1
    handle = next(iter(runtime.retry_handles))

    holder.invalidate()

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert handle.cancelled()
    assert not runtime.retry_handles


@pytest.mark.asyncio
async def test_reload_uses_next_lifecycle_epoch(hass):
    holder = RuntimeHolder.for_hass(hass)
    first = holder.create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=FakeStore(), client=FakeClient([])
    )
    holder.invalidate()
    second = holder.create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=FakeStore(), client=FakeClient([])
    )

    assert first.lifecycle_epoch == 0
    assert second.lifecycle_epoch == 1
    assert second.is_current()


@pytest.mark.asyncio
async def test_concurrent_provision_calls_share_one_setup(hass):
    gate = asyncio.Event()

    class BlockingClient(FakeClient):
        async def async_setup(self, mac):
            self.calls += 1
            await gate.wait()
            return SetupCredentials("api-key", "friendly-id")

    store = FakeStore()
    client = BlockingClient([])
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry(ENTRY_DATA), store=store, client=client
    )
    first = asyncio.create_task(runtime.async_provision())
    second = asyncio.create_task(runtime.async_provision())
    for _ in range(3):
        await asyncio.sleep(0)
    assert client.calls == 1
    gate.set()

    assert await first == await second
    assert client.calls == 1
