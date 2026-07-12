"""Validated persistent identity-registry storage for Larapaper Bridge."""

from __future__ import annotations

import asyncio
from collections import abc
from contextlib import asynccontextmanager
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, IDENTITY_VERSION, STORE_KEY, STORE_VERSION

REGISTRY_VERSION = 2
_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_PENDING_KEYS = frozenset({"version", "mac"})
_COMPLETE_KEYS = frozenset({"version", "mac", "api_key", "friendly_id"})
_REGISTRY_KEYS = frozenset({"version", "devices"})
_STORE_LOCK_KEY = f"{DOMAIN}_store_lock"
_STORE_CLAIMS_KEY = f"{DOMAIN}_flow_claims"
_STORE_REMOVED_KEY = f"{DOMAIN}_removed_identities"


class InvalidStoredState(ValueError):
    """Raised when the Larapaper identity payload is malformed."""


class IdentityAlreadyConfigured(ValueError):
    """Raised when a MAC belongs to an existing config entry."""


class IdentityRemovedError(RuntimeError):
    """Raised when stale provisioning writes a removed identity."""


@dataclass(frozen=True)
class IdentitySelection:
    """Identity selected and claimed by a config flow."""

    mac: str
    identity: dict[str, Any]


def canonicalize_mac(value: str) -> str:
    """Return an uppercase canonical MAC address."""
    if not isinstance(value, str):
        raise ValueError("MAC must be a string")
    mac = value.strip().upper()
    if not _MAC_RE.fullmatch(mac):
        raise ValueError("MAC must contain six hexadecimal octets")
    return mac


def validate_identity_payload(data: Any) -> dict[str, Any]:
    """Validate and return an exact pending or complete identity payload."""
    if not isinstance(data, abc.Mapping):
        raise InvalidStoredState("identity payload must be an object")
    keys = frozenset(data)
    if keys not in (_PENDING_KEYS, _COMPLETE_KEYS):
        raise InvalidStoredState("identity payload has unexpected fields")
    if data.get("version") != IDENTITY_VERSION or isinstance(
        data.get("version"), bool
    ):
        raise InvalidStoredState("unsupported identity payload version")
    try:
        mac = canonicalize_mac(data["mac"])
    except (KeyError, ValueError) as err:
        raise InvalidStoredState("invalid identity MAC") from err
    payload: dict[str, Any] = {"version": IDENTITY_VERSION, "mac": mac}
    if keys == _COMPLETE_KEYS:
        for field in ("api_key", "friendly_id"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise InvalidStoredState(f"invalid {field}")
            payload[field] = value.strip()
    return payload


def validate_registry_payload(data: Any) -> dict[str, Any]:
    """Validate the exact version-2 device registry payload."""
    if not isinstance(data, abc.Mapping) or frozenset(data) != _REGISTRY_KEYS:
        raise InvalidStoredState("identity registry has unexpected fields")
    if data.get("version") != REGISTRY_VERSION or isinstance(
        data.get("version"), bool
    ):
        raise InvalidStoredState("unsupported identity registry version")
    devices = data.get("devices")
    if not isinstance(devices, abc.Mapping):
        raise InvalidStoredState("identity registry devices must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for key, raw_identity in devices.items():
        try:
            mac = canonicalize_mac(key)
        except ValueError as err:
            raise InvalidStoredState("identity registry key is not canonical") from err
        if key != mac or mac in normalized:
            raise InvalidStoredState("identity registry key is not canonical")
        identity = validate_identity_payload(raw_identity)
        if identity["mac"] != mac:
            raise InvalidStoredState("identity registry MAC mismatch")
        normalized[mac] = identity
    return {"version": REGISTRY_VERSION, "devices": normalized}


class LarapaperStore:
    """Single adapter for the domain-wide Home Assistant Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORE_VERSION,
            STORE_KEY,
            private=True,
            atomic_writes=True,
        )
        lock = hass.data.get(_STORE_LOCK_KEY)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            hass.data[_STORE_LOCK_KEY] = lock
        self._lock = lock
        claims = hass.data.get(_STORE_CLAIMS_KEY)
        if not isinstance(claims, set):
            claims = set()
            hass.data[_STORE_CLAIMS_KEY] = claims
        self._active_flow_claims: set[str] = claims
        removed = hass.data.get(_STORE_REMOVED_KEY)
        if not isinstance(removed, set):
            removed = set()
            hass.data[_STORE_REMOVED_KEY] = removed
        self._removed_identities: set[str] = removed

    async def async_load(self) -> dict[str, Any] | None:
        """Load and validate the version-2 identity registry."""
        async with self._lock:
            raw = await self._store.async_load()
            if raw is None:
                return None
            return validate_registry_payload(raw)

    async def async_load_identity(self, mac: str) -> dict[str, Any] | None:
        """Load one validated identity from the version-2 registry."""
        canonical_mac = canonicalize_mac(mac)
        async with self._lock:
            registry = await self._async_load_registry_unlocked()
            identity = registry["devices"].get(canonical_mac)
            return dict(identity) if identity is not None else None

    @asynccontextmanager
    async def async_flow_transaction(self) -> abc.AsyncIterator[_RegistryTransaction]:
        """Serialize flow selection and creation against registry updates."""
        async with self._lock:
            registry = await self._async_load_registry_unlocked()
            transaction = _RegistryTransaction(self, registry)
            try:
                yield transaction
            except BaseException:
                transaction.release()
                raise

    async def async_save_pending(self, mac: str) -> None:
        """Persist a pending identity without replacing other devices."""
        canonical_mac = canonicalize_mac(mac)
        async with self._lock:
            if canonical_mac in self._removed_identities:
                raise IdentityRemovedError(canonical_mac)
            registry = await self._async_load_registry_unlocked()
            devices = registry["devices"]
            if canonical_mac not in devices:
                devices[canonical_mac] = {
                    "version": IDENTITY_VERSION,
                    "mac": canonical_mac,
                }
            await self._async_save_registry_unlocked(registry)

    async def async_save_complete(
        self, mac: str, api_key: str, friendly_id: str
    ) -> None:
        """Persist complete credentials without replacing other devices."""
        canonical_mac = canonicalize_mac(mac)
        identity = validate_identity_payload(
            {
                "version": IDENTITY_VERSION,
                "mac": canonical_mac,
                "api_key": api_key,
                "friendly_id": friendly_id,
            }
        )
        async with self._lock:
            if canonical_mac in self._removed_identities:
                raise IdentityRemovedError(canonical_mac)
            registry = await self._async_load_registry_unlocked()
            registry["devices"][canonical_mac] = identity
            await self._async_save_registry_unlocked(registry)

    async def async_migrate_v1(self, mac: str) -> dict[str, Any]:
        """Migrate the matching V1 identity into a one-record registry."""
        canonical_mac = canonicalize_mac(mac)
        async with self._lock:
            raw = await self._store.async_load()
            if raw is None:
                raise InvalidStoredState("missing identity state")
            try:
                registry = validate_registry_payload(raw)
            except InvalidStoredState:
                identity = validate_identity_payload(raw)
                if identity["mac"] != canonical_mac:
                    raise InvalidStoredState("identity migration MAC mismatch")
                registry = {
                    "version": REGISTRY_VERSION,
                    "devices": {canonical_mac: identity},
                }
                await self._async_save_registry_unlocked(registry)
                return identity
            devices = registry["devices"]
            if set(devices) != {canonical_mac}:
                raise InvalidStoredState("identity migration has conflicting records")
            return dict(devices[canonical_mac])

    async def async_remove_identity(self, mac: str) -> None:
        """Atomically remove one device while preserving all others."""
        canonical_mac = canonicalize_mac(mac)
        async with self._lock:
            self._active_flow_claims.discard(canonical_mac)
            self._removed_identities.add(canonical_mac)
            registry = await self._async_load_registry_unlocked()
            if canonical_mac not in registry["devices"]:
                return
            del registry["devices"][canonical_mac]
            await self._async_save_registry_unlocked(registry)

    async def _async_load_registry_unlocked(self) -> dict[str, Any]:
        raw = await self._store.async_load()
        if raw is None:
            return {"version": REGISTRY_VERSION, "devices": {}}
        return validate_registry_payload(raw)

    async def _async_save_registry_unlocked(self, registry: dict[str, Any]) -> None:
        await self._store.async_save(validate_registry_payload(registry))

class _RegistryTransaction:
    """Hold the store lock while a flow claims an identity."""

    def __init__(self, store: LarapaperStore, registry: dict[str, Any]) -> None:
        self._store = store
        self._registry = registry
        self._claimed_mac: str | None = None

    async def async_claim(
        self,
        configured_mac: str | None,
        configured_unique_ids: abc.Iterable[str],
        generate_mac: abc.Callable[[], str],
    ) -> IdentitySelection:
        """Select and persist a MAC before the flow creates its entry."""
        configured = set(configured_unique_ids)
        self._store._active_flow_claims.difference_update(configured)
        if configured_mac is not None:
            if (
                configured_mac in configured
                or configured_mac in self._store._active_flow_claims
            ):
                raise IdentityAlreadyConfigured(configured_mac)
            mac = configured_mac
        else:
            available = sorted(
                mac
                for mac in self._registry["devices"]
                if mac not in configured
                and mac not in self._store._active_flow_claims
            )
            if available:
                mac = available[0]
            else:
                while True:
                    mac = canonicalize_mac(generate_mac())
                    if (
                        mac not in configured
                        and mac not in self._registry["devices"]
                        and mac not in self._store._active_flow_claims
                    ):
                        break
        self._store._removed_identities.discard(mac)
        self._store._active_flow_claims.add(mac)
        self._claimed_mac = mac
        identity = self._registry["devices"].get(mac)
        if identity is None:
            identity = {"version": IDENTITY_VERSION, "mac": mac}
            self._registry["devices"][mac] = identity
            await self._store._async_save_registry_unlocked(self._registry)
        return IdentitySelection(mac, dict(identity))


    def release(self) -> None:
        """Release a claim when entry creation did not complete."""
        if self._claimed_mac is not None:
            self._store._active_flow_claims.discard(self._claimed_mac)
            self._claimed_mac = None
