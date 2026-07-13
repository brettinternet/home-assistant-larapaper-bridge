"""Larapaper Bridge Home Assistant integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr

from .config_flow import async_migrate_entry
from .const import (
    CONF_BASE_URL,
    CONF_IMAGE_BASE_URL,
    CONF_MAC,
    CONF_MAX_IMAGE_BYTES,
    DOMAIN,
)
from .image import async_create_image_operation
from .runtime import EntryRuntime, RuntimeHolder
from .provisioning import ProvisioningStateError
from .scheduler import DisplayScheduler
from .storage import InvalidStoredState, LarapaperStore, canonicalize_mac
PLATFORMS = [Platform.CAMERA]


async def _async_initialize_entry(hass: HomeAssistant, runtime: EntryRuntime) -> None:
    """Finish provisioning and start the scheduler in an owned task."""
    try:
        credentials = await runtime.async_provision()
        if not runtime.is_current():
            return
        hass.config_entries.async_update_entry(
            runtime.config_entry,
            title=credentials["friendly_id"],
        )
        registry = dr.async_get(hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, runtime.mac)}
        )
        if device is not None:
            registry.async_update_device(
                device.id,
                name=credentials["friendly_id"],
                manufacturer="Larapaper",
            )
        runtime.notify_camera_state()
        image_operation = await async_create_image_operation(
            hass,
            entry_id=runtime.entry_id,
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
        runtime.notify_camera_state()
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
        await runtime.async_validate_persisted_state()
    except (InvalidStoredState, ProvisioningStateError) as error:
        if holder.get_entry_runtime(entry) is runtime:
            holder.invalidate_entry(entry, expected_runtime=runtime)
        raise ConfigEntryError("invalid persisted Larapaper identity") from error
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if holder.get_entry_runtime(entry) is runtime:
            holder.invalidate_entry(entry, expected_runtime=runtime)
        raise
    task = runtime.create_task(_async_initialize_entry(hass, runtime))
    task.add_done_callback(_consume_initialization_result)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fence the entry before unloading its platforms."""
    holder = hass.data.get(DOMAIN)
    if isinstance(holder, RuntimeHolder):
        runtime = holder.get_entry_runtime(entry)
        if runtime is not None and runtime.config_entry is entry:
            holder.invalidate_entry(entry, expected_runtime=runtime)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only the entry's identity from the shared registry."""
    identity = getattr(entry, "unique_id", None)
    holder = hass.data.get(DOMAIN)
    if isinstance(holder, RuntimeHolder):
        runtime = holder.get_entry_runtime(entry)
        if runtime is not None and runtime.config_entry is entry:
            holder.invalidate_entry(entry, expected_runtime=runtime)
    try:
        mac = canonicalize_mac(identity) if identity else None
    except ValueError:
        mac = None
    if mac is None:
        try:
            mac = canonicalize_mac(entry.data[CONF_MAC])
        except (KeyError, ValueError):
            return
    await LarapaperStore(hass).async_remove_identity(mac)


__all__ = [
    "PLATFORMS",
    "async_migrate_entry",
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
]
