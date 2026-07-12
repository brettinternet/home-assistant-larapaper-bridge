"""Store adapter and version-2 identity-registry tests."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.larapaper_bridge import storage
from custom_components.larapaper_bridge.const import DOMAIN


MAC_A = "AA:BB:CC:DD:EE:FF"
MAC_B = "11:22:33:44:55:66"


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
        self.loaded = data


def _patch_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    _FakeStore.instances.clear()
    monkeypatch.setattr(storage, "Store", _FakeStore)
    return _FakeStore


def _identity(mac: str, **extra: str) -> dict[str, object]:
    return {"version": 1, "mac": mac, **extra}


def _registry(*identities: dict[str, object]) -> dict[str, object]:
    return {
        "version": 2,
        "devices": {identity["mac"]: identity for identity in identities},
    }


def test_store_uses_fixed_private_atomic_key_and_envelope_version(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)

    storage.LarapaperStore(hass)

    instance = fake_store.instances[0]
    assert instance.args == (hass, 1, DOMAIN)
    assert instance.kwargs == {"private": True, "atomic_writes": True}


@pytest.mark.asyncio
async def test_store_saves_exact_registry_and_preserves_other_devices(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)

    await adapter.async_save_pending(MAC_A)
    await adapter.async_save_complete(MAC_B, " api-key ", " friendly-id ")

    assert fake_store.instances[0].saved == [
        _registry(_identity(MAC_A)),
        _registry(
            _identity(MAC_A),
            _identity(MAC_B, api_key="api-key", friendly_id="friendly-id"),
        ),
    ]

@pytest.mark.asyncio
async def test_concurrent_complete_writes_preserve_unrelated_devices(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    first = storage.LarapaperStore(hass)
    second = storage.LarapaperStore(hass)
    second._store = fake_store.instances[0]

    await asyncio.gather(
        first.async_save_complete(MAC_A, "key-a", "name-a"),
        second.async_save_complete(MAC_B, "key-b", "name-b"),
    )

    assert fake_store.instances[0].loaded == _registry(
        _identity(MAC_A, api_key="key-a", friendly_id="name-a"),
        _identity(MAC_B, api_key="key-b", friendly_id="name-b"),
    )



@pytest.mark.asyncio
async def test_complete_identity_survives_adapter_recreation(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    first = storage.LarapaperStore(hass)
    await first.async_save_complete(MAC_A, "api-key", "friendly-id")

    second = storage.LarapaperStore(hass)
    second._store = fake_store.instances[0]
    assert await second.async_load_identity(MAC_A) == _identity(
        MAC_A, api_key="api-key", friendly_id="friendly-id"
    )
@pytest.mark.asyncio
async def test_store_loads_registry_and_exposes_one_identity(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    adapter._store.loaded = _registry(
        _identity(MAC_A),
        _identity(MAC_B, api_key="key", friendly_id="name"),
    )

    assert await adapter.async_load() == _registry(
        _identity(MAC_A),
        _identity(MAC_B, api_key="key", friendly_id="name"),
    )
    assert await adapter.async_load_identity(MAC_B.lower()) == _identity(
        MAC_B, api_key="key", friendly_id="name"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "mac": MAC_A},
        {"version": 2, "devices": {MAC_A.lower(): _identity(MAC_A)}},
        {"version": 2, "devices": {MAC_A: _identity(MAC_B)}},
        {"version": 2, "devices": {MAC_A: {"version": 1, "mac": MAC_A, "extra": 1}}},
        {"version": 3, "devices": {}},
        {"version": 2, "devices": []},
        {"version": 2, "devices": {MAC_A: {"version": 2, "mac": MAC_A}}},
    ],
)
async def test_store_rejects_v1_and_malformed_registry(
    hass, monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    adapter._store.loaded = payload

    with pytest.raises(storage.InvalidStoredState):
        await adapter.async_load()


@pytest.mark.asyncio
async def test_registry_transaction_serializes_concurrent_same_mac_claims(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_claim():
        async with adapter.async_flow_transaction() as transaction:
            result = await transaction.async_claim(MAC_A, set(), lambda: MAC_A)
            entered.set()
            await release.wait()
            return result

    first = asyncio.create_task(first_claim())
    await entered.wait()

    async def second_claim():
        async with adapter.async_flow_transaction() as transaction:
            return await transaction.async_claim(MAC_A, set(), lambda: MAC_A)

    second = asyncio.create_task(second_claim())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    assert (await first).mac == MAC_A
    with pytest.raises(storage.IdentityAlreadyConfigured):
        await second


@pytest.mark.asyncio
async def test_migration_rewrites_only_matching_v1_identity(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake_store.instances[0].loaded = _identity(
        MAC_A, api_key="key", friendly_id="name"
    )

    assert await adapter.async_migrate_v1(MAC_A) == _identity(
        MAC_A, api_key="key", friendly_id="name"
    )
    assert fake_store.instances[0].saved == [
        _registry(_identity(MAC_A, api_key="key", friendly_id="name"))
    ]

@pytest.mark.asyncio
async def test_migration_rewrites_pending_v1_identity(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake_store.instances[0].loaded = _identity(MAC_A)

    assert await adapter.async_migrate_v1(MAC_A) == _identity(MAC_A)
    assert fake_store.instances[0].saved == [_registry(_identity(MAC_A))]


@pytest.mark.asyncio
async def test_migration_accepts_matching_registry_retry_without_write(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake_store.instances[0].loaded = _registry(_identity(MAC_A))

    assert await adapter.async_migrate_v1(MAC_A) == _identity(MAC_A)
    assert fake_store.instances[0].saved == []


@pytest.mark.asyncio
async def test_migration_rejects_mismatch_without_mutation(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake_store.instances[0].loaded = _identity(MAC_B)

    with pytest.raises(storage.InvalidStoredState):
        await adapter.async_migrate_v1(MAC_A)
    assert fake_store.instances[0].saved == []


@pytest.mark.asyncio
async def test_remove_identity_preserves_other_devices(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    fake_store.instances[0].loaded = _registry(_identity(MAC_A), _identity(MAC_B))

    await adapter.async_remove_identity(MAC_A)
    assert fake_store.instances[0].saved == [_registry(_identity(MAC_B))]

@pytest.mark.asyncio
async def test_removed_identity_rejects_late_provisioning_write(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_store = _patch_store(monkeypatch)
    adapter = storage.LarapaperStore(hass)
    await adapter.async_save_pending(MAC_A)
    await adapter.async_remove_identity(MAC_A)

    with pytest.raises(storage.IdentityRemovedError):
        await adapter.async_save_complete(MAC_A, "late-key", "late-name")
    assert fake_store.instances[0].loaded == {"version": 2, "devices": {}}
