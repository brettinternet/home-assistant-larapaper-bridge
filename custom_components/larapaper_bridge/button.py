"""Native manual Larapaper display refresh button."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import EntryRuntime, RuntimeHolder


class LarapaperBridgeRefreshButton(ButtonEntity):
    """Request a display cycle through the entry-owned scheduler."""

    _attr_name = "Refresh display"
    _attr_should_poll = False

    def __init__(self, runtime: EntryRuntime) -> None:
        """Initialize the stateless refresh projection."""
        super().__init__()
        self._runtime = runtime
        self._attr_unique_id = f"{DOMAIN}_{runtime.mac}_refresh_display"
        self._attr_device_info = runtime.device_info

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return true only while provisioning and the scheduler are ready."""
        scheduler = self._runtime.scheduler
        return (
            self._runtime.is_current()
            and scheduler is not None
            and scheduler.is_running
        )

    async def async_press(self) -> None:
        """Coalesce the press through the scheduler-owned refresh API."""
        if not self.available:
            return
        scheduler = self._runtime.scheduler
        if scheduler is not None:
            await scheduler.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[LarapaperBridgeRefreshButton]], None],
) -> None:
    """Add one refresh button for the existing entry runtime."""
    holder = hass.data.get(DOMAIN)
    if not isinstance(holder, RuntimeHolder):
        return
    runtime = holder.get_entry_runtime(entry)
    if runtime is None:
        return
    button = LarapaperBridgeRefreshButton(runtime)
    runtime.button_entity = button
    async_add_entities([button])


__all__ = ["LarapaperBridgeRefreshButton", "async_setup_entry"]
