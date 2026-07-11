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
        if current is not None:
            current._invalidate()
        self.lifecycle_epoch += 1


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
        self._provisioner = Provisioner(
            store=store,
            client=client,
            sleep=sleep,
            register_retry_handle=self.register_retry_handle,
            unregister_retry_handle=self.unregister_retry_handle,
        )
        self._provision_task: asyncio.Task[dict[str, Any]] | None = None

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
