"""Store adapter contract and identity payload validation tests."""

from __future__ import annotations

import pytest

from custom_components.larapaper_bridge import storage
from custom_components.larapaper_bridge.const import DOMAIN


class _FakeStore:
    """Capture HA Store construction and payload writes."""

    instances: list[_FakeStore] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.loaded = None
        self.saved: list[dict[str, object]] = []
        self.instances.append(self)

    async def async_load(self):
        return self.loaded

    async def async_save(self, data):
        self.saved.append(data)


def _patch_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    _FakeStore.instances.clear()
    monkeypatch.setattr(storage, "Store", _FakeStore)
    return _FakeStore


def test_store_uses_fixed_private_atomic_key_and_envelope_version(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)

    storage.LarapaperStore(hass)

    instance = fake_store.instances[0]
    assert instance.args == (hass, 1, DOMAIN)
    assert instance.kwargs == {"private": True, "atomic_writes": True}


@pytest.mark.asyncio
async def test_store_saves_exact_pending_and_complete_payloads(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)

    await adapter.async_save_pending("aa:bb:cc:dd:ee:ff")
    await adapter.async_save_complete(
        "AA:BB:CC:DD:EE:FF", "  api-key  ", " friendly-id "
    )

    assert fake_store.instances[0].saved == [
        {"version": 1, "mac": "AA:BB:CC:DD:EE:FF"},
        {
            "version": 1,
            "mac": "AA:BB:CC:DD:EE:FF",
            "api_key": "api-key",
            "friendly_id": "friendly-id",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "mac": "AA:BB:CC:DD:EE:FF"},
        {"version": 1, "mac": "AA:BB:CC:DD:EE:FF", "unexpected": True},
        {"version": 1},
        {"version": 1, "mac": "AA-BB-CC-DD-EE-FF"},
        {
            "version": 1,
            "mac": "AA:BB:CC:DD:EE:FF",
            "api_key": "",
            "friendly_id": "friendly-id",
        },
        {
            "version": 1,
            "mac": "AA:BB:CC:DD:EE:FF",
            "api_key": "api-key",
        },
    ],
)
async def test_store_rejects_unsupported_or_malformed_payloads(
    hass, monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    fake_store = _patch_store(monkeypatch)
    fake_store.instances.clear()
    fake = _FakeStore()
    fake.loaded = payload
    adapter = storage.LarapaperStore(hass)
    adapter._store = fake

    with pytest.raises(storage.InvalidStoredState):
        await adapter.async_load()


@pytest.mark.asyncio
async def test_store_loads_exact_pending_and_complete_shapes(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake = _FakeStore()
    adapter._store = fake

    fake.loaded = {"version": 1, "mac": "aa:bb:cc:dd:ee:ff"}
    assert await adapter.async_load() == {
        "version": 1,
        "mac": "AA:BB:CC:DD:EE:FF",
    }

    fake.loaded = {
        "version": 1,
        "mac": "AA:BB:CC:DD:EE:FF",
        "api_key": " api-key ",
        "friendly_id": " friendly-id ",
    }
    assert await adapter.async_load() == {
        "version": 1,
        "mac": "AA:BB:CC:DD:EE:FF",
        "api_key": "api-key",
        "friendly_id": "friendly-id",
    }
