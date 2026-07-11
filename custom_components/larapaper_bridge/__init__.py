"""Larapaper Bridge Home Assistant integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BASE_URL,
    CONF_IMAGE_BASE_URL,
    CONF_MAX_IMAGE_BYTES,
    DOMAIN,
)
from .image import async_create_image_operation
from .runtime import EntryRuntime, RuntimeHolder
from .scheduler import DisplayScheduler

PLATFORMS = [Platform.CAMERA]


async def _async_initialize_entry(hass: HomeAssistant, runtime: EntryRuntime) -> None:
    """Finish provisioning and start the scheduler in an owned task."""
    try:
        credentials = await runtime.async_provision()
        if not runtime.is_current():
            return
        image_operation = await async_create_image_operation(
            hass,
            larapaper_base_url=runtime.config_entry.data[CONF_BASE_URL],
            image_base_url=runtime.config_entry.data.get(CONF_IMAGE_BASE_URL),
            max_image_bytes=runtime.config_entry.data[CONF_MAX_IMAGE_BYTES],
        )
        if not runtime.is_current():
            return
        scheduler = DisplayScheduler(
            runtime,
            api_key=credentials["api_key"],
            image_operation=image_operation,
        )
        scheduler.async_start()
    except asyncio.CancelledError:
        raise
    except Exception:
        if runtime.is_current():
            scheduler = DisplayScheduler(runtime, api_key="")
            scheduler.last_error = "setup_failed"


def _consume_initialization_result(task: asyncio.Task[None]) -> None:
    """Consume unexpected task failures without leaking them to asyncio."""
    if not task.cancelled():
        task.exception()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the camera and own provisioning/scheduler background work."""
    holder = RuntimeHolder.for_hass(hass)
    runtime = holder.create_entry_runtime(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if holder.current is runtime:
            holder.invalidate()
        raise
    task = runtime.create_task(_async_initialize_entry(hass, runtime))
    task.add_done_callback(_consume_initialization_result)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fence the entry before unloading its platforms."""
    holder = hass.data.get(DOMAIN)
    if isinstance(holder, RuntimeHolder):
        runtime = holder.current
        if runtime is not None and runtime.config_entry is entry:
            holder.invalidate()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

__all__ = ["PLATFORMS", "async_setup_entry", "async_unload_entry"]
