"""Config-flow validation and multi-device identity tests."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_USER

import custom_components.larapaper_bridge.config_flow as config_flow
from custom_components.larapaper_bridge.config_flow import async_migrate_entry
from custom_components.larapaper_bridge.const import (
    CONF_BASE_URL,
    CONF_IMAGE_BASE_URL,
    CONF_MAC,
    CONF_MAX_IMAGE_BYTES,
    CONF_MAX_STALE_SECONDS,
    CONF_MIN_POLL_SECONDS,
    DOMAIN,
)
from custom_components.larapaper_bridge.storage import (
    InvalidStoredState,
    LarapaperStore,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


BASE_INPUT = {
    CONF_BASE_URL: "  https://example.test/bridge///  ",
    CONF_IMAGE_BASE_URL: " https://images.example.test/ ",
    CONF_MAC: "aa:bb:cc:dd:ee:ff",
    CONF_MIN_POLL_SECONDS: "12.5",
    CONF_MAX_STALE_SECONDS: "7200",
    CONF_MAX_IMAGE_BYTES: "2048",
}


async def _start_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    return result["flow_id"]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (CONF_BASE_URL, "ftp://example.test", "invalid_base_url"),
        (CONF_BASE_URL, "https://user:pass@example.test", "invalid_base_url"),
        (CONF_IMAGE_BASE_URL, "https://images.test/path", "invalid_image_base_url"),
        (CONF_MAC, "aa-bb-cc-dd-ee-ff", "invalid_mac"),
        (CONF_MIN_POLL_SECONDS, "nan", "invalid_min_poll_seconds"),
        (CONF_MIN_POLL_SECONDS, "0", "invalid_min_poll_seconds"),
        (CONF_MAX_STALE_SECONDS, "1.5", "invalid_max_stale_seconds"),
        (CONF_MAX_IMAGE_BYTES, "-1", "invalid_max_image_bytes"),
    ],
)
async def test_invalid_input_is_rejected(hass, field, value, error):
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={**BASE_INPUT, field: value}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"][field] == error


async def test_normalizes_input_and_persists_pending_identity(hass):
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input=BASE_INPUT
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "AA:BB:CC:DD:EE:FF"
    assert result["data"] == {
        CONF_BASE_URL: "https://example.test/bridge/",
        CONF_IMAGE_BASE_URL: "https://images.example.test",
        CONF_MAC: "AA:BB:CC:DD:EE:FF",
        CONF_MIN_POLL_SECONDS: 12.5,
        CONF_MAX_STALE_SECONDS: 7200,
        CONF_MAX_IMAGE_BYTES: 2048,
    }
    assert result["result"].unique_id == "AA:BB:CC:DD:EE:FF"
    assert await LarapaperStore(hass).async_load() == {
        "version": 2,
        "devices": {
            "AA:BB:CC:DD:EE:FF": {
                "version": 1,
                "mac": "AA:BB:CC:DD:EE:FF",
            }
        },
    }


async def test_defaults_and_generated_mac(hass):
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_BASE_URL] == "https://example.test/"
    assert data[CONF_IMAGE_BASE_URL] is None
    assert data[CONF_MIN_POLL_SECONDS] == 60.0
    assert data[CONF_MAX_STALE_SECONDS] == 3600
    assert data[CONF_MAX_IMAGE_BYTES] == 10485760
    first_octet = int(data[CONF_MAC].split(":", 1)[0], 16)
    assert first_octet & 0x03 == 0x02
    assert data[CONF_MAC].upper() == data[CONF_MAC]


async def test_two_entries_have_distinct_mac_ids_and_settings(hass):
    first = await _start_flow(hass)
    first_result = await hass.config_entries.flow.async_configure(
        first,
        user_input={
            CONF_BASE_URL: "https://one.example",
            CONF_MAC: "AA:BB:CC:DD:EE:FF",
        },
    )
    second = await _start_flow(hass)
    second_result = await hass.config_entries.flow.async_configure(
        second,
        user_input={
            CONF_BASE_URL: "https://two.example",
            CONF_MAC: "11:22:33:44:55:66",
        },
    )

    assert first_result["result"].unique_id == "AA:BB:CC:DD:EE:FF"
    assert second_result["result"].unique_id == "11:22:33:44:55:66"
    assert second_result["data"][CONF_BASE_URL] == "https://two.example/"
    assert set((await LarapaperStore(hass).async_load())["devices"]) == {
        "AA:BB:CC:DD:EE:FF",
        "11:22:33:44:55:66",
    }


async def test_duplicate_mac_aborts_before_registry_mutation(hass):
    first = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(
        first,
        user_input={
            CONF_BASE_URL: "https://one.example",
            CONF_MAC: "AA:BB:CC:DD:EE:FF",
        },
    )

    second = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        second,
        user_input={
            CONF_BASE_URL: "https://two.example",
            CONF_MAC: "aa:bb:cc:dd:ee:ff",
        },
    )
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert set((await LarapaperStore(hass).async_load())["devices"]) == {
        "AA:BB:CC:DD:EE:FF"
    }

async def test_concurrent_supplied_mac_flows_create_one_entry(hass):
    first_flow, second_flow = await asyncio.gather(
        _start_flow(hass),
        _start_flow(hass),
    )
    results = await asyncio.gather(
        hass.config_entries.flow.async_configure(
            first_flow,
            user_input={
                CONF_BASE_URL: "https://one.example",
                CONF_MAC: "AA:BB:CC:DD:EE:FF",
            },
        ),
        hass.config_entries.flow.async_configure(
            second_flow,
            user_input={
                CONF_BASE_URL: "https://two.example",
                CONF_MAC: "AA:BB:CC:DD:EE:FF",
            },
        ),
        return_exceptions=True,
    )
    successful = [
        result
        for result in results
        if isinstance(result, dict)
        and result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    ]
    aborted = [
        result
        for result in results
        if isinstance(result, dict)
        and result["type"] is data_entry_flow.FlowResultType.ABORT
    ]
    assert len(successful) == 1
    assert len(aborted) == 1
    assert aborted[0]["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_omitted_mac_reuses_lexically_first_unclaimed_record(hass):
    store = LarapaperStore(hass)
    await store.async_save_pending("BB:BB:BB:BB:BB:BB")
    await store.async_save_complete(
        "AA:AA:AA:AA:AA:AA", "api-key", "friendly-id"
    )

    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )
    assert result["data"][CONF_MAC] == "AA:AA:AA:AA:AA:AA"


async def test_generated_mac_retries_registry_and_entry_collisions(
    hass, monkeypatch: pytest.MonkeyPatch
):
    for mac in ("AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"):
        flow_id = await _start_flow(hass)
        await hass.config_entries.flow.async_configure(
            flow_id,
            user_input={CONF_BASE_URL: "https://example.test", CONF_MAC: mac},
        )

    generated = iter(("AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB", "CC:CC:CC:CC:CC:CC"))
    monkeypatch.setattr(config_flow, "_generate_mac", lambda: next(generated))
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )
    assert result["data"][CONF_MAC] == "CC:CC:CC:CC:CC:CC"


async def test_interrupted_flow_reuses_pending_registry_claim(hass, monkeypatch):
    original_create_entry = config_flow.LarapaperBridgeConfigFlow.async_create_entry

    def fail_create_entry(self, **_kwargs):
        raise RuntimeError("interrupted before entry creation")

    monkeypatch.setattr(
        config_flow.LarapaperBridgeConfigFlow,
        "async_create_entry",
        fail_create_entry,
    )
    flow_id = await _start_flow(hass)
    with pytest.raises(RuntimeError, match="interrupted"):
        await hass.config_entries.flow.async_configure(
            flow_id,
            user_input={
                CONF_BASE_URL: "https://example.test",
                CONF_MAC: "AA:BB:CC:DD:EE:FF",
            },
        )
    hass.config_entries.flow.async_abort(flow_id)
    monkeypatch.setattr(
        config_flow.LarapaperBridgeConfigFlow,
        "async_create_entry",
        original_create_entry,
    )

    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )
    assert result["data"][CONF_MAC] == "AA:BB:CC:DD:EE:FF"


class _FakeStore:
    def __init__(self, loaded):
        self.loaded = loaded
        self.saved: list[dict[str, object]] = []

    async def async_load(self):
        return self.loaded

    async def async_save(self, data):
        self.saved.append(data)
        self.loaded = data


async def test_migration_rewrites_v1_and_updates_entry(hass, monkeypatch):
    store = LarapaperStore(hass)
    fake = _FakeStore(
        {"version": 1, "mac": "AA:BB:CC:DD:EE:FF", "api_key": "key", "friendly_id": "name"}
    )
    store._store = fake
    monkeypatch.setattr(config_flow, "LarapaperStore", lambda _hass: store)
    updates = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_update_entry",
        lambda entry, **kwargs: updates.append((entry, kwargs)),
    )

    class Entry:
        version = 1
        unique_id = DOMAIN
        data = {CONF_MAC: "aa:bb:cc:dd:ee:ff"}

    entry = Entry()
    assert await async_migrate_entry(hass, entry) is True
    assert fake.saved == [
        {
            "version": 2,
            "devices": {
                "AA:BB:CC:DD:EE:FF": {
                    "version": 1,
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "api_key": "key",
                    "friendly_id": "name",
                }
            },
        }
    ]
    assert updates == [
        (
            entry,
            {
                "version": 2,
                "unique_id": "AA:BB:CC:DD:EE:FF",
                "title": "name",
            },
        )
    ]


async def test_migration_rejects_conflicting_registry_without_mutation(hass):
    store = LarapaperStore(hass)
    fake = _FakeStore(
        {
            "version": 2,
            "devices": {
                "AA:BB:CC:DD:EE:FF": {
                    "version": 1,
                    "mac": "AA:BB:CC:DD:EE:FF",
                },
                "11:22:33:44:55:66": {
                    "version": 1,
                    "mac": "11:22:33:44:55:66",
                },
            },
        }
    )
    store._store = fake

    class Entry:
        version = 1
        unique_id = DOMAIN
        data = {CONF_MAC: "AA:BB:CC:DD:EE:FF"}

    with pytest.raises(InvalidStoredState):
        await async_migrate_entry(hass, Entry())
    assert fake.saved == []
