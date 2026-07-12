"""Focused tests for Home Assistant entry setup and unload wiring."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryError

import custom_components.larapaper_bridge as integration
from custom_components.larapaper_bridge.const import DOMAIN
from custom_components.larapaper_bridge.storage import InvalidStoredState, LarapaperStore


MAC = "AA:BB:CC:DD:EE:FF"
ENTRY_DATA = {
    "base_url": "https://example.test/bridge/",
    "image_base_url": "https://images.example.test",
    "mac": MAC,
    "min_poll_seconds": 60.0,
    "max_stale_seconds": 3600,
    "max_image_bytes": 1024,
}


@dataclass
class FakeEntry:
    entry_id: str
    data: dict[str, object]


class FakeRuntime:
    def __init__(self, holder: FakeHolder, entry: FakeEntry) -> None:
        self.holder = holder
        self.config_entry = entry
        self.scheduler: Any | None = None
        self.provision_calls = 0
        self.invalidated = False
        self.tasks: list[asyncio.Task[Any]] = []

    async def async_validate_persisted_state(self) -> None:
        if self.holder.validation_error is not None:
            raise self.holder.validation_error

    async def async_provision(self) -> dict[str, str]:
        self.provision_calls += 1
        if self.holder.provision_future is not None:
            await self.holder.provision_future
        if self.holder.provision_error is not None:
            raise self.holder.provision_error
        return {"api_key": "secret-api-key", "friendly_id": "display-1"}
    def create_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task
    def is_current(self) -> bool:
        return self.holder.current is self and not self.invalidated


class FakeHolder:
    provision_error: Exception | None = None
    provision_future: asyncio.Future[None] | None = None
    validation_error: Exception | None = None
    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.current: FakeRuntime | None = None
        self.invalidations = 0

    @classmethod
    def for_hass(cls, hass: Any) -> FakeHolder:
        holder = hass.data.get(DOMAIN)
        if not isinstance(holder, cls):
            holder = cls(hass)
            hass.data[DOMAIN] = holder
        return holder

    def create_entry_runtime(self, entry: FakeEntry) -> FakeRuntime:
        runtime = FakeRuntime(self, entry)
        self.current = runtime
        return runtime
    def invalidate(self) -> None:
        self.invalidations += 1
        if self.current is not None:
            self.current.invalidated = True
            for task in self.current.tasks:
                task.cancel()
        self.current = None


class FakeImageOperation:
    pass
class FakeScheduler:
    instances: list[FakeScheduler] = []

    def __init__(self, runtime: FakeRuntime, *, api_key: str, image_operation: Any = None) -> None:
        self.runtime = runtime
        self.api_key = api_key
        self.image_operation = image_operation
        self.started = False
        self.last_error: str | None = None
        runtime.scheduler = self
        self.instances.append(self)

    def async_start(self) -> None:
        self.started = True


@pytest.fixture
def setup_fakes(hass, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(integration, "RuntimeHolder", FakeHolder)
    monkeypatch.setattr(integration, "DisplayScheduler", FakeScheduler)
    monkeypatch.setattr(
        hass.config_entries,
        "async_update_entry",
        lambda entry, **kwargs: entry.__dict__.update(kwargs),
    )
    FakeHolder.provision_error = None
    FakeHolder.provision_future = None
    FakeHolder.validation_error = None
    FakeScheduler.instances.clear()
    return FakeHolder


@pytest.mark.asyncio
async def test_setup_provisions_starts_scheduler_and_forwards_camera(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    image_calls: list[dict[str, object]] = []
    forwarded: list[tuple[object, list[object]]] = []

    async def create_image_operation(hass, **kwargs):
        image_calls.append({"hass": hass, **kwargs})
        return FakeImageOperation()

    async def forward_entry_setups(forwarded_entry, platforms):
        forwarded.append((forwarded_entry, platforms))

    monkeypatch.setattr(integration, "async_create_image_operation", create_image_operation)
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )


    assert await integration.async_setup_entry(hass, entry) is True
    await asyncio.sleep(0)

    holder = hass.data[DOMAIN]
    runtime = holder.current
    assert runtime is not None
    assert runtime.provision_calls == 1
    assert entry.title == "display-1"
    assert FakeScheduler.instances[0].api_key == "secret-api-key"
    assert FakeScheduler.instances[0].started is True
    assert image_calls == [
        {
            "hass": hass,
            "larapaper_base_url": ENTRY_DATA["base_url"],
            "image_base_url": ENTRY_DATA["image_base_url"],
            "max_image_bytes": ENTRY_DATA["max_image_bytes"],
        }
    ]
    assert len(forwarded) == 1
    assert forwarded[0][0] is entry
    assert [platform.value for platform in forwarded[0][1]] == ["camera"]


@pytest.mark.asyncio
async def test_unload_forwards_platform_unload_and_invalidates_runtime(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    unloaded: list[tuple[object, list[object]]] = []

    async def forward_entry_setups(_entry, _platforms):
        return None

    async def unload_platforms(unloaded_entry, platforms):
        assert runtime.invalidated is True
        unloaded.append((unloaded_entry, platforms))
        return True
    async def create_image_operation(_hass, **_kwargs):
        return FakeImageOperation()

    monkeypatch.setattr(integration, "async_create_image_operation", create_image_operation)

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        unload_platforms,
    )

    await integration.async_setup_entry(hass, entry)
    holder = hass.data[DOMAIN]
    runtime = holder.current
    assert runtime is not None
    await asyncio.sleep(0)
    assert isinstance(holder, setup_fakes)
    assert holder.current.config_entry is entry

    assert await integration.async_unload_entry(hass, entry) is True

    assert runtime.config_entry is entry
    assert unloaded[0][0] is entry
    assert [platform.value for platform in unloaded[0][1]] == ["camera"]
    assert holder.current is None
    assert runtime.invalidated is True
    assert holder.invalidations == 1


@pytest.mark.asyncio
async def test_unload_invalidates_runtime_when_platform_unload_fails(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)

    async def forward_entry_setups(_entry, _platforms):
        return None

    async def unload_platforms(_entry, _platforms):
        raise RuntimeError("platform unload failed")

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        unload_platforms,
    )

    await integration.async_setup_entry(hass, entry)
    holder = hass.data[DOMAIN]
    runtime = holder.current
    assert runtime is not None

    with pytest.raises(RuntimeError, match="platform unload failed"):
        await integration.async_unload_entry(hass, entry)

    assert holder.current is None
    assert runtime.invalidated is True


@pytest.mark.asyncio
async def test_setup_returns_while_provisioning_is_deferred_and_unload_cancels(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    setup_fakes.provision_future = asyncio.get_running_loop().create_future()

    async def forward_entry_setups(_entry, _platforms):
        return None

    async def unload_platforms(_entry, _platforms):
        return True

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        unload_platforms,
    )

    assert await integration.async_setup_entry(hass, entry) is True
    await asyncio.sleep(0)

    holder = hass.data[DOMAIN]
    runtime = holder.current
    assert runtime is not None
    assert runtime.provision_calls == 1
    assert runtime.tasks and not runtime.tasks[0].done()

    await integration.async_unload_entry(hass, entry)
    await asyncio.sleep(0)

    assert runtime.tasks[0].cancelled()
    assert holder.current is None


@pytest.mark.asyncio
async def test_setup_failure_exposes_safe_error_projection(hass, monkeypatch, setup_fakes):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    setup_fakes.provision_error = RuntimeError("setup failed")

    async def forward_entry_setups(_entry, _platforms):
        return None

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )

    assert await integration.async_setup_entry(hass, entry) is True
    await asyncio.sleep(0)

    assert FakeScheduler.instances[-1].last_error == "setup_failed"

@pytest.mark.asyncio
async def test_image_resource_failure_does_not_start_display_scheduler(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)

    async def forward_entry_setups(_entry, _platforms):
        return None

    async def create_image_operation(_hass, **_kwargs):
        raise RuntimeError("image resources unavailable")

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )
    monkeypatch.setattr(
        integration,
        "async_create_image_operation",
        create_image_operation,
    )

    assert await integration.async_setup_entry(hass, entry) is True
    await asyncio.sleep(0)

    scheduler = FakeScheduler.instances[-1]
    assert scheduler.last_error == "setup_failed"
    assert scheduler.started is False

@pytest.mark.asyncio
async def test_invalid_persisted_state_raises_config_entry_error(
    hass, monkeypatch, setup_fakes
):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    setup_fakes.validation_error = InvalidStoredState("invalid stored state")
    forwarded = False

    async def forward_entry_setups(_entry, _platforms):
        nonlocal forwarded
        forwarded = True

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        forward_entry_setups,
    )

    with pytest.raises(ConfigEntryError, match="invalid persisted Larapaper identity"):
        await integration.async_setup_entry(hass, entry)

    holder = hass.data[DOMAIN]
    assert holder.current is None
    assert holder.invalidations == 1
    assert forwarded is False

@pytest.mark.asyncio
async def test_remove_entry_deletes_only_its_registry_record(hass):
    store = LarapaperStore(hass)
    await store.async_save_pending("AA:BB:CC:DD:EE:FF")
    await store.async_save_pending("11:22:33:44:55:66")

    entry = FakeEntry("entry-1", {**ENTRY_DATA, "mac": "AA:BB:CC:DD:EE:FF"})
    entry.unique_id = DOMAIN
    await integration.async_remove_entry(hass, entry)

    assert await store.async_load() == {
        "version": 2,
        "devices": {
            "11:22:33:44:55:66": {
                "version": 1,
                "mac": "11:22:33:44:55:66",
            }
        },
    }

@pytest.mark.asyncio
async def test_remove_entry_invalidates_active_runtime_before_registry_removal(
    hass, monkeypatch, setup_fakes
):
    holder = setup_fakes.for_hass(hass)
    entry = FakeEntry("entry-1", ENTRY_DATA)
    runtime = holder.create_entry_runtime(entry)
    removed: list[str] = []

    class FakeRegistryStore:
        async def async_remove_identity(self, mac):
            removed.append(mac)

    monkeypatch.setattr(
        integration, "LarapaperStore", lambda _hass: FakeRegistryStore()
    )
    await integration.async_remove_entry(hass, entry)

    assert runtime.invalidated is True
    assert removed == [MAC]
