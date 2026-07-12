"""Provision one stable Larapaper identity with bounded retries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .client import LarapaperClient, LarapaperClientError
from .storage import InvalidStoredState, LarapaperStore, validate_identity_payload

RETRY_DELAYS_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)


class ProvisioningStateError(ValueError):
    """Raised when persisted identity state cannot serve the requested MAC."""


class ProvisioningInvalidatedError(RuntimeError):
    """Raised when an entry is unloaded while provisioning is in progress."""


Sleep = Callable[[float], Awaitable[None]]
IsActive = Callable[[], bool]
ReportError = Callable[[str | None], None]
RegisterRetryHandle = Callable[[asyncio.TimerHandle], None]
UnregisterRetryHandle = Callable[[asyncio.TimerHandle], None]


class Provisioner:
    """Run setup attempts for one identity, without owning lifecycle state."""

    def __init__(
        self,
        *,
        store: LarapaperStore,
        client: LarapaperClient,
        sleep: Sleep | None = None,
        report_error: ReportError | None = None,
        register_retry_handle: RegisterRetryHandle | None = None,
        unregister_retry_handle: UnregisterRetryHandle | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._sleep = sleep
        self._report_error = report_error
        self._register_retry_handle = register_retry_handle
        self._unregister_retry_handle = unregister_retry_handle

    async def async_validate_stored_state(
        self, mac: str, is_active: IsActive
    ) -> None:
        """Validate persisted identity state without starting provisioning."""
        await self._load_state(mac, is_active)

    async def async_provision(self, mac: str, is_active: IsActive) -> dict[str, Any]:
        """Load, provision, and persist one identity until success or invalidation."""

        state = await self._load_state(mac, is_active)
        if state is not None and "api_key" in state:
            return state
        if state is None:
            await self._ensure_active(is_active)
            await self._store.async_save_pending(mac)

        retry_index = 0
        while True:
            await self._ensure_active(is_active)
            try:
                credentials = await self._client.async_setup(mac)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._report_error is not None:
                    code = (
                        error.code
                        if isinstance(error, LarapaperClientError)
                        else "setup_failed"
                    )
                    self._report_error(code)
                delay = RETRY_DELAYS_SECONDS[
                    min(retry_index, len(RETRY_DELAYS_SECONDS) - 1)
                ]
                retry_index += 1
                await self._wait_retry(delay)
                state = await self._load_state(mac, is_active)
                if state is not None and "api_key" in state:
                    if self._report_error is not None:
                        self._report_error(None)
                    return state
                if state is None:
                    await self._ensure_active(is_active)
                    await self._store.async_save_pending(mac)
                continue

            await self._ensure_active(is_active)
            complete = {
                "version": 1,
                "mac": mac,
                "api_key": credentials.api_key,
                "friendly_id": credentials.friendly_id,
            }
            await self._store.async_save_complete(
                complete["mac"], complete["api_key"], complete["friendly_id"]
            )
            if self._report_error is not None:
                self._report_error(None)
            return validate_identity_payload(complete)

    async def _wait_retry(self, delay: float) -> None:
        if self._sleep is not None:
            await self._sleep(delay)
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        handle = loop.call_later(delay, waiter.set_result, None)
        if self._register_retry_handle is not None:
            self._register_retry_handle(handle)
        try:
            await waiter
        finally:
            handle.cancel()
            if self._unregister_retry_handle is not None:
                self._unregister_retry_handle(handle)

    async def _load_state(
        self, mac: str, is_active: IsActive
    ) -> dict[str, Any] | None:
        await self._ensure_active(is_active)
        try:
            raw_state = await self._store.async_load()
        except asyncio.CancelledError:
            raise
        except InvalidStoredState:
            raise
        await self._ensure_active(is_active)
        if raw_state is None:
            return None
        try:
            state = validate_identity_payload(raw_state)
        except InvalidStoredState:
            raise
        if state["mac"] != mac:
            raise ProvisioningStateError("persisted identity MAC does not match config entry")
        return state

    @staticmethod
    async def _ensure_active(is_active: IsActive) -> None:
        if not is_active():
            raise ProvisioningInvalidatedError("config entry is no longer active")
