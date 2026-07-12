"""Lifecycle fencing and task ownership for the Larapaper integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .client import LarapaperClient
from .const import CONF_BASE_URL, CONF_MAC, CONF_MIN_POLL_SECONDS, DOMAIN
from .provisioning import Provisioner
from .storage import LarapaperStore

Sleep = Callable[[float], Awaitable[None]]


class RuntimeHolder:
    """Own domain-scoped state that survives config-entry reloads."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.lifecycle_epoch = 0
        self.image_resources: Any | None = None
        self.current: EntryRuntime | None = None

    @classmethod
    def for_hass(cls, hass: Any) -> RuntimeHolder:
        """Return the one holder stored in Home Assistant domain data."""
        holder = hass.data.get(DOMAIN)
        if not isinstance(holder, cls):
            holder = cls(hass)
            hass.data[DOMAIN] = holder
        return holder

    def create_entry_runtime(
        self,
        config_entry: Any,
        *,
        store: LarapaperStore | None = None,
        client: LarapaperClient | None = None,
        sleep: Sleep | None = None,
    ) -> EntryRuntime:
        """Create the current coordinator at the holder's current epoch."""
        if self.current is not None:
            self.invalidate()
        runtime = EntryRuntime(
            holder=self,
            config_entry=config_entry,
            store=store or LarapaperStore(self.hass),
            client=client
            or LarapaperClient(
                self.hass,
                config_entry.data[CONF_BASE_URL],
                config_entry.data[CONF_MIN_POLL_SECONDS],
            ),
            sleep=sleep,
        )
        self.current = runtime
        return runtime

    def invalidate(self) -> None:
        """Fence and cancel the current entry, then advance the epoch."""
        current = self.current
        self.current = None
        self.lifecycle_epoch += 1
        if current is not None:
            current._invalidate()

class EntryRuntime:
    """Own one entry's cancellable tasks and provisioning operation."""

    def __init__(
        self,
        *,
        holder: RuntimeHolder,
        config_entry: Any,
        store: LarapaperStore,
        client: LarapaperClient,
        sleep: Sleep | None = None,
    ) -> None:
        self.holder = holder
        self.config_entry = config_entry
        self.store = store
        self.client = client
        self.mac = config_entry.data[CONF_MAC]
        self.lifecycle_epoch = holder.lifecycle_epoch
        self._cycle_generation = 0
        self.stopped = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.retry_handles: set[asyncio.TimerHandle] = set()
        self.scheduler: Any | None = None
        self.camera_entity: Any | None = None
        self._provisioner = Provisioner(
            store=store,
            client=client,
            sleep=sleep,
            report_error=self._set_provisioning_error,
            register_retry_handle=self.register_retry_handle,
            unregister_retry_handle=self.unregister_retry_handle,
        )
        self.provisioning_error: str | None = None
        self._provision_task: asyncio.Task[dict[str, Any]] | None = None

    def notify_camera_state(self) -> None:
        """Write the cache-backed camera state on the Home Assistant loop."""
        if self.is_current() and self.camera_entity is not None:
            self.camera_entity.async_write_ha_state()

    def is_current(self) -> bool:
        """Return whether this runtime may still mutate entry state."""
        return (
            not self.stopped
            and self.holder.current is self
            and self.holder.lifecycle_epoch == self.lifecycle_epoch
        )
    def begin_cycle(self):
        """Advance the cycle generation and return its fencing token."""
        from .scheduler import OperationToken

        self._cycle_generation += 1
        return OperationToken(self.lifecycle_epoch, self._cycle_generation)

    def is_token_current(self, token: Any) -> bool:
        """Return whether a cycle token may still mutate runtime state."""
        return (
            self.is_current()
            and token.lifecycle_epoch == self.lifecycle_epoch
            and token.cycle_generation == self._cycle_generation
        )

    def create_task(self, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        """Create and register a runtime-owned task for unload cancellation."""
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @property
    def cycle_generation(self) -> int:
        """Return the current display-cycle generation."""
        return self._cycle_generation

    async def async_validate_persisted_state(self) -> None:
        """Validate persisted identity before forwarding platform setup."""
        await self._provisioner.async_validate_stored_state(
            self.mac, self.is_current
        )

    def _set_provisioning_error(self, error_code: str | None) -> None:
        """Expose a safe provisioning error while retries remain active."""
        if self.is_current():
            self.provisioning_error = error_code

    async def async_provision(self) -> dict[str, Any]:
        """Provision once; concurrent callers share the same operation."""
        if not self.is_current():
            raise RuntimeError("config entry is not active")
        task = self._provision_task
        if task is None or task.done():
            task = self._create_task(
                self._provisioner.async_provision(self.mac, self.is_current)
            )
            self._provision_task = task
        try:
            return await task
        finally:
            if task.done() and self._provision_task is task:
                self._provision_task = None

    def register_retry_handle(self, handle: asyncio.TimerHandle) -> None:
        """Register a provisioning retry handle for unload cancellation."""
        if self.stopped:
            handle.cancel()
            return
        self.retry_handles.add(handle)

    def unregister_retry_handle(self, handle: asyncio.TimerHandle) -> None:
        """Drop a settled retry handle from the runtime registry."""
        self.retry_handles.discard(handle)

    def _create_task(self, awaitable: Awaitable[dict[str, Any]]) -> asyncio.Task[dict[str, Any]]:
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _invalidate(self) -> None:
        """Stop this runtime without waiting for cooperative cancellation."""
        if self.stopped:
            return
        self.stopped = True
        if self.scheduler is not None:
            self.scheduler.stop()
        for task in tuple(self.tasks):
            task.cancel()
        for handle in tuple(self.retry_handles):
            handle.cancel()
        self.tasks.clear()
        self.retry_handles.clear()
        self._provision_task = None


def async_get_runtime_holder(hass: Any) -> RuntimeHolder:
    """Return the domain runtime holder for integration setup code."""
    return RuntimeHolder.for_hass(hass)
