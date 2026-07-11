"""Config-flow validation and pending identity tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import SOURCE_USER

from custom_components.larapaper_bridge.const import (
    CONF_BASE_URL,
    CONF_IMAGE_BASE_URL,
    CONF_MAC,
    CONF_MAX_IMAGE_BYTES,
    CONF_MAX_STALE_SECONDS,
    CONF_MIN_POLL_SECONDS,
    DOMAIN,
)
from custom_components.larapaper_bridge.storage import LarapaperStore


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
    invalid = {**BASE_INPUT, field: value}
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input=invalid
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"][field] == error


async def test_normalizes_input_and_persists_pending_identity(hass):
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input=BASE_INPUT
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Larapaper Bridge"
    assert result["data"] == {
        CONF_BASE_URL: "https://example.test/bridge/",
        CONF_IMAGE_BASE_URL: "https://images.example.test",
        CONF_MAC: "AA:BB:CC:DD:EE:FF",
        CONF_MIN_POLL_SECONDS: 12.5,
        CONF_MAX_STALE_SECONDS: 7200,
        CONF_MAX_IMAGE_BYTES: 2048,
    }
    assert result["result"].unique_id == DOMAIN

    state = await LarapaperStore(hass).async_load()
    assert state == {"version": 1, "mac": "AA:BB:CC:DD:EE:FF"}


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


async def test_pending_mac_is_reused(hass):
    await LarapaperStore(hass).async_save_pending("AA:BB:CC:DD:EE:FF")
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAC] == "AA:BB:CC:DD:EE:FF"


async def test_duplicate_unique_id_aborts_before_store_mutation(hass):
    flow_id = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={CONF_BASE_URL: "https://example.test"}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
