"""Focused tests for the redacted diagnostics projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.larapaper_bridge.diagnostics import (
    async_get_config_entry_diagnostics,
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
EXPECTED_KEYS = {
    "status",
    "ready",
    "stale",
    "last_success_at",
    "last_success_age_seconds",
    "last_error",
    "next_display_at",
    "next_retry_at",
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
        raise AssertionError("provisioning is not part of diagnostics tests")

    async def async_display(self, _mac: str, _api_key: str):
        raise AssertionError("diagnostics must not call /api/display")


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def diagnostics_runtime(hass):
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        FakeEntry("entry-1", ENTRY_DATA),
        store=FakeStore(),
        client=FakeClient(),
    )
    clock = FakeClock()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    scheduler = DisplayScheduler(
        runtime,
        api_key="secret-api-key",
        clock=clock,
        utc_now=lambda: now,
    )
    yield runtime, scheduler, clock, now
    runtime.holder.invalidate_entry("entry-1")


@pytest.mark.asyncio
async def test_diagnostics_returns_cold_start_projection_without_mutating_hass(hass):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    before = dict(hass.data)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result == {
        "status": "starting",
        "ready": False,
        "stale": False,
        "last_success_at": None,
        "last_success_age_seconds": None,
        "last_error": None,
        "next_display_at": None,
        "next_retry_at": None,
    }
    assert dict(hass.data) == before
    json.dumps(result)


@pytest.mark.asyncio
async def test_diagnostics_preserves_ready_precedence_and_serializes_projections(
    diagnostics_runtime,
):
    runtime, scheduler, clock, now = diagnostics_runtime
    received_at = now - timedelta(seconds=2)
    scheduler.cache_record = CacheRecord(
        png_bytes=b"png",
        received_monotonic=100.0,
        received_at=received_at,
    )
    scheduler.last_error = "display_failed"
    scheduler.next_display_deadline = 160.0
    scheduler.next_retry_deadline = 105.0

    result = await async_get_config_entry_diagnostics(
        runtime.holder.hass, runtime.config_entry
    )

    assert set(result) == EXPECTED_KEYS
    assert result["status"] == "ready"
    assert result["ready"] is True
    assert result["stale"] is False
    assert result["last_success_at"] == received_at.isoformat()
    assert result["last_success_age_seconds"] == 0
    assert result["last_error"] == "display_failed"
    assert result["next_display_at"] == (now + timedelta(seconds=60)).isoformat()
    assert result["next_retry_at"] == (now + timedelta(seconds=5)).isoformat()
    assert clock.value == 100.0
    json.dumps(result)


@pytest.mark.asyncio
async def test_diagnostics_uses_stale_boundary_and_collapses_unknown_errors(
    diagnostics_runtime,
):
    runtime, scheduler, clock, _now = diagnostics_runtime
    scheduler.cache_record = CacheRecord(
        png_bytes=b"png",
        received_monotonic=100.0,
        received_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    scheduler.last_error = "https://secret.example/?api_key=secret"
    clock.value = 110.0

    result = await async_get_config_entry_diagnostics(
        runtime.holder.hass, runtime.config_entry
    )

    assert result["status"] == "stale"
    assert result["ready"] is False
    assert result["stale"] is True
    assert result["last_success_age_seconds"] == 10
    assert result["last_error"] == "internal_error"
    assert "secret.example" not in json.dumps(result)


@pytest.mark.asyncio
async def test_diagnostics_reports_error_without_serveable_cache(diagnostics_runtime):
    runtime, scheduler, _clock, _now = diagnostics_runtime
    scheduler.last_error = "image_fetch_failed"

    first = await async_get_config_entry_diagnostics(
        runtime.holder.hass, runtime.config_entry
    )
    second = await async_get_config_entry_diagnostics(
        runtime.holder.hass, runtime.config_entry
    )

    assert first == second
    assert first["status"] == "error"
    assert first["ready"] is False
    assert first["stale"] is False
    assert first["last_error"] == "image_fetch_failed"

@pytest.mark.asyncio
async def test_diagnostics_reports_provisioning_error_before_scheduler_exists(hass):
    entry = FakeEntry("entry-1", ENTRY_DATA)
    runtime = RuntimeHolder.for_hass(hass).create_entry_runtime(
        entry,
        store=FakeStore(),
        client=FakeClient(),
    )
    runtime.provisioning_error = "setup_auto_assign_disabled"

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["status"] == "error"
    assert result["ready"] is False
    assert result["last_error"] == "setup_auto_assign_disabled"
    runtime.holder.invalidate_entry("entry-1")

@pytest.mark.asyncio
async def test_diagnostics_selects_only_requested_entry_runtime(hass):
    second_mac = "11:22:33:44:55:66"
    first_entry = FakeEntry("entry-1", ENTRY_DATA)
    second_entry = FakeEntry(
        "entry-2",
        {**ENTRY_DATA, "mac": second_mac},
    )
    holder = RuntimeHolder.for_hass(hass)
    first = holder.create_entry_runtime(
        first_entry, store=FakeStore(), client=FakeClient()
    )
    second = holder.create_entry_runtime(
        second_entry, store=FakeStore(), client=FakeClient()
    )
    DisplayScheduler(first, api_key="first")
    second_scheduler = DisplayScheduler(second, api_key="second")
    second_scheduler.last_error = "display_failed"

    first_result = await async_get_config_entry_diagnostics(hass, first_entry)
    second_result = await async_get_config_entry_diagnostics(hass, second_entry)

    assert first_result["status"] == "starting"
    assert first_result["last_error"] is None
    assert second_result["status"] == "error"
    assert second_result["last_error"] == "display_failed"

    holder.invalidate_entry("entry-1")
    holder.invalidate_entry("entry-2")
