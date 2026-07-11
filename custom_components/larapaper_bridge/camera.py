"""Native cache-only Larapaper camera entity."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import EntryRuntime, RuntimeHolder


class LarapaperBridgeCamera(Camera):
    """Expose the scheduler's last-good PNG without performing I/O."""

    _attr_name = "Larapaper Display"
    _attr_should_poll = False

    def __init__(self, runtime: EntryRuntime) -> None:
        """Initialize the cache-only camera projection."""
        super().__init__()
        self._runtime = runtime
        entry = runtime.config_entry
        entry_identity = getattr(entry, "entry_id", runtime.mac)
        self._attr_unique_id = f"{DOMAIN}_{entry_identity}_camera"
        self.content_type = "image/png"

    @property
    def available(self) -> bool:
        """Return true only while a fresh image can be served."""
        if not self._runtime.is_current():
            return False
        scheduler = self._runtime.scheduler
        return scheduler is not None and scheduler.is_cache_fresh()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the immutable cached PNG without scheduling work."""
        del width, height
        if not self._runtime.is_current():
            return None
        scheduler = self._runtime.scheduler
        if scheduler is None:
            return None
        return scheduler.cached_image()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[LarapaperBridgeCamera]], None],
) -> None:
    """Add the camera for the already-created entry runtime."""
    holder = hass.data.get(DOMAIN)
    if not isinstance(holder, RuntimeHolder) or holder.current is None:
        return
    runtime = holder.current
    if runtime.config_entry is not entry:
        return
    async_add_entities([LarapaperBridgeCamera(runtime)])


__all__ = ["LarapaperBridgeCamera", "async_setup_entry"]
